"""
HTTP layer of the Remote API.

A small JSON API for other programs on the computer or local network (scripts,
dashboards, automation tools) to read machine status, follow the console, list
files on the SD card and, if allowed in settings, upload G-code.

The API cannot start jobs, move the machine, change settings or send MDI
commands. Those stay with the person at the Controller.

This module has no Kivy dependency. Everything that needs the running
application goes through a :class:`MachineBackend`, so the HTTP handling can be
tested without a machine or a window.

Endpoints (all JSON, all except ``/health`` need ``Authorization: Bearer <token>``):

``GET  /api/v1/health``                 API and Controller version
``GET  /api/v1/status``                 machine state, positions, tool, job progress
``GET  /api/v1/console?since=N&limit=M`` console lines received after sequence number N
``GET  /api/v1/machine-config``         machine configuration values loaded by the Controller
``GET  /api/v1/files?path=/sd/gcodes``  list a folder on the SD card
``PUT  /api/v1/files?path=/sd/gcodes/x.nc``  upload the request body (if enabled)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import posixpath
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

API_VERSION = 1
API_PREFIX = "/api/v1"
SD_ROOT = "/sd"
UPLOAD_ROOT = "/sd/gcodes"
DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
CONSOLE_DEFAULT_LIMIT = 200
CONSOLE_MAX_LIMIT = 1000


class ApiError(Exception):
    """An error with an HTTP status, reported to the client as ``{"error": message}``."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = message


class MachineBackend(Protocol):
    """What the HTTP layer needs from the running Controller."""

    def status(self) -> dict[str, Any]: ...

    def console(self, since: int, limit: int) -> dict[str, Any]: ...

    def machine_config(self) -> dict[str, str] | None: ...

    def list_dir(self, path: str) -> list[dict[str, Any]]: ...

    def upload(self, data: bytes, remote_path: str) -> dict[str, Any]: ...


def normalize_machine_path(path: str | None, root: str) -> str:
    """Return *path* normalised, or raise ApiError if it is not *root* or inside it.

    Only absolute POSIX-style paths are accepted; ``..`` cannot climb out of *root*.
    """
    if not path:
        raise ApiError(HTTPStatus.BAD_REQUEST, "missing 'path'")
    if not path.startswith("/") or "\\" in path or any(ord(c) < 32 for c in path):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid path: {path!r}")
    norm = posixpath.normpath(path)
    if norm != root and not norm.startswith(root.rstrip("/") + "/"):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"path must be inside {root}")
    return norm


def validate_upload_path(path: str | None) -> str:
    """Return the normalised upload target, a file name inside ``/sd/gcodes``."""
    if path and path.endswith("/"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "path must name a file, not a folder")
    norm = normalize_machine_path(path, UPLOAD_ROOT)
    name = posixpath.basename(norm)
    if norm == UPLOAD_ROOT or not name or name.startswith("."):
        raise ApiError(HTTPStatus.BAD_REQUEST, "path must name a file inside /sd/gcodes")
    return norm


def _int_param(query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = query.get(name, [None])[0]
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"'{name}' must be an integer")
    if value < minimum or value > maximum:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"'{name}' must be between {minimum} and {maximum}")
    return value


class RemoteApiServer:
    """Serves the API on its own threads until :meth:`stop` is called."""

    def __init__(
        self,
        backend: MachineBackend,
        *,
        host: str,
        port: int,
        token: str,
        allow_upload: bool = False,
        controller_version: str = "",
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ):
        if not token:
            raise ValueError("an access token is required")
        self.backend = backend
        self.host = host
        self.port = int(port)
        self.token = token
        self.allow_upload = bool(allow_upload)
        self.controller_version = controller_version
        self.max_upload_bytes = int(max_upload_bytes)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        """The bound (host, port), or None if not running. Port 0 resolves to the real port."""
        if self._httpd is None:
            return None
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = ThreadingHTTPServer((self.host, self.port), _RequestHandler)
        httpd.daemon_threads = True
        httpd.api = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="remote-api", daemon=True)
        self._thread.start()
        logger.info("Remote API: listening on %s port %d", *self.address)  # type: ignore[misc]

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = self._thread = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        logger.info("Remote API: stopped")

    def check_token(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer ") :].strip().encode(), self.token.encode())


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "CarveraControllerRemoteAPI"
    protocol_version = "HTTP/1.1"
    timeout = 60

    @property
    def api(self) -> RemoteApiServer:
        return self.server.api  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
        logger.debug("Remote API: %s - %s", self.address_string(), format % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        self._body_read = False
        try:
            parts = urlsplit(self.path)
            route = parts.path.rstrip("/") or "/"
            query = parse_qs(parts.query)
            routes = {
                f"{API_PREFIX}/health": {"GET": self._health},
                f"{API_PREFIX}/status": {"GET": self._status},
                f"{API_PREFIX}/console": {"GET": self._console},
                f"{API_PREFIX}/machine-config": {"GET": self._machine_config},
                f"{API_PREFIX}/files": {"GET": self._list_files, "PUT": self._upload_file},
            }
            handlers = routes.get(route)
            if handlers is None:
                raise ApiError(HTTPStatus.NOT_FOUND, f"unknown endpoint: {route}")
            handler = handlers.get(method)
            if handler is None:
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, f"{method} not allowed on {route}")
            if route != f"{API_PREFIX}/health" and not self.api.check_token(self.headers.get("Authorization")):
                raise ApiError(HTTPStatus.UNAUTHORIZED, "missing or wrong access token")
            self._send_json(HTTPStatus.OK, handler(query))
        except ApiError as e:
            self._send_json(e.status, {"error": e.message})
        except Exception:
            logger.exception("Remote API: request failed: %s %s", method, self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error, see Controller log"})

    # ------------------------------------------------------------------ routes
    def _health(self, query):
        return {"api_version": API_VERSION, "controller_version": self.api.controller_version}

    def _status(self, query):
        return self.api.backend.status()

    def _console(self, query):
        since = _int_param(query, "since", 0, 0, 2**63 - 1)
        limit = _int_param(query, "limit", CONSOLE_DEFAULT_LIMIT, 1, CONSOLE_MAX_LIMIT)
        return self.api.backend.console(since, limit)

    def _machine_config(self, query):
        config = self.api.backend.machine_config()
        if config is None:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "machine configuration not loaded yet")
        return {"config": config}

    def _list_files(self, query):
        path = normalize_machine_path(query.get("path", [UPLOAD_ROOT])[0], SD_ROOT)
        return {"path": path, "files": self.api.backend.list_dir(path)}

    def _upload_file(self, query):
        if not self.api.allow_upload:
            raise ApiError(HTTPStatus.FORBIDDEN, "uploads are disabled in the Controller's Remote API settings")
        path = validate_upload_path(query.get("path", [None])[0])
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            length = int(length_header)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "empty upload")
        if length > self.api.max_upload_bytes:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"upload exceeds {self.api.max_upload_bytes} bytes")
        data = self.rfile.read(length)
        self._body_read = True
        if len(data) != length:
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body shorter than Content-Length")
        expected_md5 = self.headers.get("X-Content-MD5")
        if expected_md5 and hashlib.md5(data).hexdigest() != expected_md5.strip().lower():
            raise ApiError(HTTPStatus.BAD_REQUEST, "body does not match X-Content-MD5")
        return self.api.backend.upload(data, path)

    # ----------------------------------------------------------------- helpers
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="Carvera Controller"')
        has_body = self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0").strip() not in (
            "",
            "0",
        )
        if has_body and not self._body_read:
            # The body was not consumed (error before reading it): on a kept-alive
            # connection it would be parsed as the next request.
            self.close_connection = True
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
