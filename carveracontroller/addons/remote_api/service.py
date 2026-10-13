"""Starts, stops and reconfigures the Remote API from the Controller settings."""

from __future__ import annotations

import logging
import secrets

from kivy.config import Config
from kivy.utils import platform as kivy_platform

from carveracontroller.Controller import Controller
from carveracontroller.serial_listeners import register_serial_listener, unregister_serial_listener

from .backend import KivyMachineBackend
from .console import ConsoleBuffer
from .server import RemoteApiServer

logger = logging.getLogger(__name__)

SECTION = "carvera"
SETTING_PREFIX = "remote_api_"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780


class RemoteApiService:
    """Owns the API server and the console listener for the running app."""

    def __init__(self, root, controller_version: str = ""):
        self._root = root
        self._controller_version = controller_version
        self._server: RemoteApiServer | None = None
        self._listener: int | None = None
        self.console = ConsoleBuffer()

    @property
    def running(self) -> bool:
        return self._server is not None

    def apply_config(self) -> None:
        """(Re)start the server to match the current settings, or stop it if disabled."""
        self.stop()
        if kivy_platform in ("android", "ios"):
            return
        if not Config.getboolean(SECTION, "remote_api_enabled", fallback=False):
            return

        token = Config.get(SECTION, "remote_api_token", fallback="").strip()
        if not token:
            token = secrets.token_urlsafe(24)
            Config.set(SECTION, "remote_api_token", token)
            Config.write()
            config_popup = getattr(self._root, "config_popup", None)
            if config_popup is not None and hasattr(config_popup, "show_value"):
                config_popup.show_value(SECTION, "remote_api_token", token)
            logger.info("Remote API: generated a new access token (see Settings > Remote API)")

        host = Config.get(SECTION, "remote_api_host", fallback=DEFAULT_HOST).strip() or DEFAULT_HOST
        try:
            port = int(float(Config.get(SECTION, "remote_api_port", fallback=str(DEFAULT_PORT))))
        except ValueError:
            port = -1
        if not 0 <= port <= 65535:
            self._report_error(
                f"Remote API: port must be between 1 and 65535, not {Config.get(SECTION, 'remote_api_port')!r}"
            )
            return
        server = RemoteApiServer(
            KivyMachineBackend(self._root, self.console),
            host=host,
            port=port,
            token=token,
            allow_upload=Config.getboolean(SECTION, "remote_api_allow_upload", fallback=False),
            controller_version=self._controller_version,
        )
        try:
            server.start()
        except (OSError, OverflowError, ValueError) as e:
            self._report_error(f"Remote API: could not listen on {host} port {port}: {e}")
            return
        self._server = server
        self._listener = register_serial_listener(self._on_serial_line)

    def stop(self) -> None:
        if self._listener is not None:
            unregister_serial_listener(self._listener)
            self._listener = None
        if self._server is not None:
            self._server.stop()
            self._server = None

    def _report_error(self, message: str) -> None:
        logger.error(message)
        self._root.controller.log.put((Controller.MSG_ERROR, message))

    def _on_serial_line(self, msg_kind: int, line: str) -> None:
        self.console.append("error" if msg_kind == Controller.MSG_ERROR else "output", line)
