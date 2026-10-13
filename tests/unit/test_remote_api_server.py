"""Tests for the Remote API HTTP layer, using a fake backend (no Kivy, no machine)."""

from __future__ import annotations

import hashlib
import http.client
import json

import pytest

from carveracontroller.addons.remote_api.server import (
    ApiError,
    RemoteApiServer,
    normalize_machine_path,
    validate_upload_path,
)

TOKEN = "test-token"


class FakeBackend:
    def __init__(self):
        self.uploads = []
        self.listed = []
        self.config = {"spindle.max_rpm": "13000"}

    def status(self):
        return {"connected": True, "state": "Idle"}

    def console(self, since, limit):
        return {"lines": [], "latest": since, "missed": False, "limit": limit}

    def machine_config(self):
        return self.config

    def list_dir(self, path):
        self.listed.append(path)
        if path == "/sd/busy":
            raise ApiError(409, "another file listing is in progress")
        return [{"name": "a.nc", "path": path + "/a.nc", "is_dir": False, "size": 3, "modified": "20261013120000"}]

    def upload(self, data, remote_path):
        self.uploads.append((remote_path, data))
        return {"path": remote_path, "bytes": len(data), "md5": hashlib.md5(data).hexdigest()}


@pytest.fixture
def api():
    backend = FakeBackend()
    server = RemoteApiServer(backend, host="127.0.0.1", port=0, token=TOKEN, allow_upload=True, max_upload_bytes=1024)
    server.start()
    yield server, backend
    server.stop()


def request(server, method, path, body=None, token=TOKEN, headers=None):
    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    payload = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, payload


def test_health_needs_no_token(api):
    server, _ = api
    status, body = request(server, "GET", "/api/v1/health", token=None)
    assert status == 200
    assert body["api_version"] == 1


@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_endpoints_reject_missing_or_wrong_token(api, token):
    server, _ = api
    status, body = request(server, "GET", "/api/v1/status", token=token)
    assert status == 401
    assert "token" in body["error"]


def test_status_and_config(api):
    server, _ = api
    assert request(server, "GET", "/api/v1/status") == (200, {"connected": True, "state": "Idle"})
    status, body = request(server, "GET", "/api/v1/machine-config")
    assert status == 200
    assert body["config"]["spindle.max_rpm"] == "13000"


def test_machine_config_not_loaded(api):
    server, backend = api
    backend.config = None
    status, _ = request(server, "GET", "/api/v1/machine-config")
    assert status == 503


def test_console_parameters(api):
    server, _ = api
    status, body = request(server, "GET", "/api/v1/console?since=5&limit=10")
    assert status == 200
    assert body["latest"] == 5 and body["limit"] == 10
    assert request(server, "GET", "/api/v1/console?since=abc")[0] == 400
    assert request(server, "GET", "/api/v1/console?limit=0")[0] == 400


def test_list_files_defaults_to_gcodes_and_stays_on_sd(api):
    server, backend = api
    status, body = request(server, "GET", "/api/v1/files")
    assert status == 200
    assert body["path"] == "/sd/gcodes"
    assert body["files"][0]["name"] == "a.nc"
    assert request(server, "GET", "/api/v1/files?path=/sd/../etc")[0] == 400
    assert request(server, "GET", "/api/v1/files?path=/tmp")[0] == 400
    assert backend.listed == ["/sd/gcodes"]


def test_backend_errors_keep_their_status(api):
    server, _ = api
    status, body = request(server, "GET", "/api/v1/files?path=/sd/busy")
    assert status == 409
    assert "in progress" in body["error"]


def test_upload(api):
    server, backend = api
    data = b"G0 X0\n"
    status, body = request(
        server,
        "PUT",
        "/api/v1/files?path=/sd/gcodes/job.nc",
        body=data,
        headers={"X-Content-MD5": hashlib.md5(data).hexdigest()},
    )
    assert status == 200
    assert body == {"path": "/sd/gcodes/job.nc", "bytes": len(data), "md5": hashlib.md5(data).hexdigest()}
    assert backend.uploads == [("/sd/gcodes/job.nc", data)]


def test_upload_md5_mismatch(api):
    server, backend = api
    status, _ = request(
        server, "PUT", "/api/v1/files?path=/sd/gcodes/job.nc", body=b"G0\n", headers={"X-Content-MD5": "0" * 32}
    )
    assert status == 400
    assert backend.uploads == []


def test_upload_too_large(api):
    server, backend = api
    status, _ = request(server, "PUT", "/api/v1/files?path=/sd/gcodes/big.nc", body=b"x" * 2048)
    assert status == 413
    assert backend.uploads == []


@pytest.mark.parametrize(
    "path", ["/sd/job.nc", "/sd/gcodes", "/sd/gcodes/", "/sd/gcodes/.hidden", "/sd/gcodes/../config.txt"]
)
def test_upload_rejects_paths_outside_gcodes(api, path):
    server, backend = api
    status, _ = request(server, "PUT", f"/api/v1/files?path={path}", body=b"G0\n")
    assert status == 400
    assert backend.uploads == []


def test_upload_disabled():
    backend = FakeBackend()
    server = RemoteApiServer(backend, host="127.0.0.1", port=0, token=TOKEN, allow_upload=False)
    server.start()
    try:
        status, body = request(server, "PUT", "/api/v1/files?path=/sd/gcodes/job.nc", body=b"G0\n")
    finally:
        server.stop()
    assert status == 403
    assert "disabled" in body["error"]
    assert backend.uploads == []


def test_unknown_route_and_method(api):
    server, _ = api
    assert request(server, "GET", "/api/v1/nope")[0] == 404
    assert request(server, "POST", "/api/v1/status", body=b"")[0] == 405
    assert request(server, "DELETE", "/api/v1/files?path=/sd/gcodes/a.nc")[0] == 405


def test_unread_body_is_not_parsed_as_a_second_request(api):
    """A refused request's body must not be read as the next request on a kept-alive connection."""
    import socket

    server, _ = api
    smuggled = f"GET /api/v1/status HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode()
    head = (
        f"PUT /api/v1/files?path=/sd/gcodes/a.nc HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer wrong\r\n"
        f"Content-Length: {len(smuggled)}\r\n\r\n"
    ).encode()
    with socket.create_connection(server.address, timeout=5) as sock:
        sock.sendall(head + smuggled)
        received = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            received += chunk
    assert received.count(b"HTTP/1.1 ") == 1
    assert received.startswith(b"HTTP/1.1 401")


def test_server_requires_token():
    with pytest.raises(ValueError):
        RemoteApiServer(FakeBackend(), host="127.0.0.1", port=0, token="")


def test_normalize_machine_path():
    assert normalize_machine_path("/sd/gcodes/sub/../x", "/sd") == "/sd/gcodes/x"
    assert normalize_machine_path("/sd", "/sd") == "/sd"
    for bad in ["", None, "sd/gcodes", "/sdx", "/sd/../x", "\\sd\\gcodes", "/sd/a\x00b"]:
        with pytest.raises(ApiError):
            normalize_machine_path(bad, "/sd")


def test_validate_upload_path():
    assert validate_upload_path("/sd/gcodes/sub/job file.nc") == "/sd/gcodes/sub/job file.nc"
    with pytest.raises(ApiError):
        validate_upload_path(None)
