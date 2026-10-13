"""
Connects the Remote API to the running Controller.

HTTP requests arrive on server threads. Anything that touches the UI or the
Controller's transfer state is handed to the Kivy main thread with
``Clock.schedule_once``; the request thread waits on an Event for the answer.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from http import HTTPStatus
from typing import Any

from kivy.app import App
from kivy.clock import Clock

from carveracontroller import Utils
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_USB, CONN_WIFI, NOT_CONNECTED, SEND_FILE, Controller
from carveracontroller.translation import tr

from .console import ConsoleBuffer
from .server import ApiError

logger = logging.getLogger(__name__)

LIST_TIMEOUT = 15.0
MAIN_THREAD_TIMEOUT = 5.0


def _vars_get(name: str, default: Any = None) -> Any:
    return CNC.vars.get(name, default)


class KivyMachineBackend:
    """:class:`~carveracontroller.addons.remote_api.server.MachineBackend` for the running app."""

    def __init__(self, root, console: ConsoleBuffer):
        self._root = root
        self._console = console

    # ----------------------------------------------------------------- helpers
    @property
    def _controller(self) -> Controller:
        return self._root.controller

    def _connected(self) -> bool:
        app = App.get_running_app()
        return self._controller.stream is not None and app is not None and app.state != NOT_CONNECTED

    def _on_main_thread(self, func, timeout: float = MAIN_THREAD_TIMEOUT):
        """Run *func* on the Kivy main thread and return its result (or raise its ApiError).

        If the main thread does not get to it within *timeout*, *func* is cancelled and
        will not run later, so a request that already failed cannot claim anything.
        """
        done = threading.Event()
        lock = threading.Lock()
        box: dict[str, Any] = {"cancelled": False}

        def run(_dt):
            with lock:
                if box["cancelled"]:
                    return
                try:
                    box["result"] = func()
                except Exception as e:
                    box["error"] = e
                finally:
                    done.set()

        Clock.schedule_once(run, 0)
        if not done.wait(timeout):
            with lock:
                if not done.is_set():
                    box["cancelled"] = True
                    raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Controller is busy, try again")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        app = App.get_running_app()
        root = self._root
        controller = self._controller
        connected = self._connected()
        connection = None
        if connected:
            connection = {CONN_WIFI: "wifi", CONN_USB: "usb"}.get(controller.connection_type, "unknown")
        return {
            "connected": connected,
            "connection": connection,
            "machine": {
                "model": getattr(app, "model", "") or None,
                "firmware_version": getattr(root, "fw_version", None) or None,
                "community_firmware": bool(getattr(app, "is_community_firmware", False)),
            },
            "state": getattr(app, "state", NOT_CONNECTED),
            "halt_reason": _vars_get("halt_reason"),
            "alarm_message": _vars_get("alarm_message") or None,
            "playing": bool(getattr(app, "playing", False)),
            "transfer_in_progress": bool(getattr(root, "uploading", False) or getattr(root, "downloading", False)),
            "job": {
                "file": getattr(app, "selected_remote_filename", "") or None,
                "lines_played": _vars_get("playedlines"),
                "percent": _vars_get("playedpercent"),
                "elapsed_seconds": _vars_get("playedseconds"),
            },
            "position": {
                "machine": {axis: _vars_get("m" + axis) for axis in ("x", "y", "z", "a")},
                "work": {axis: _vars_get("w" + axis) for axis in ("x", "y", "z", "a")},
                "wcs": _vars_get("active_coord_system"),
            },
            "tool": {"number": _vars_get("tool"), "tlo": _vars_get("tlo"), "target": _vars_get("target_tool")},
            "spindle": {
                "rpm": _vars_get("curspindle"),
                "target_rpm": _vars_get("tarspindle"),
                "override_percent": _vars_get("OvSpindle"),
                "temperature": _vars_get("spindletemp"),
            },
            "feed": {
                "current": _vars_get("curfeed"),
                "target": _vars_get("tarfeed"),
                "override_percent": _vars_get("OvFeed"),
            },
            "modes": {
                "vacuum": _vars_get("vacuummode"),
                "ext_out": _vars_get("extoutmode"),
                "auto_blow": _vars_get("autoblowmode"),
                "auto_bed_clean": _vars_get("autobedcleanmode"),
                "ionizer": _vars_get("ionizermode"),
                "laser": _vars_get("lasermode"),
            },
            "switches": {
                "e_stop": _vars_get("st_e_stop"),
                "cover": _vars_get("st_cover"),
                "probe": _vars_get("st_probe"),
                "tool_sensor": _vars_get("st_tool_sensor"),
            },
        }

    # ----------------------------------------------------------------- console
    def console(self, since: int, limit: int) -> dict[str, Any]:
        return self._console.since(since, limit)

    # ------------------------------------------------------------------ config
    def machine_config(self) -> dict[str, str] | None:
        if not getattr(self._root, "config_loaded", False):
            return None
        return {str(k): str(v) for k, v in dict(self._root.setting_list).items()}

    # ------------------------------------------------------------------- files
    def _ensure_free(self, action: str) -> None:
        """Raise 409 unless the machine is connected, idle and nothing else uses the file channel.

        Main thread only. These are the conditions the file browser and firmware updater
        check, plus the gaps inside multi-step operations (batches, config backup,
        decompression after an upload) where the per-file flags are briefly clear.
        """
        app = App.get_running_app()
        root = self._root
        controller = self._controller
        if not self._connected():
            raise ApiError(HTTPStatus.CONFLICT, "not connected to a machine")
        if app.state != "Idle" or app.playing:
            raise ApiError(HTTPStatus.CONFLICT, f"machine must be idle to {action} (state: {app.state})")
        if (
            root.uploading
            or root.downloading
            or getattr(root, "downloading_config", False)
            or getattr(root, "backing_up_config", False)
            or getattr(root, "decompstatus", False)
            or getattr(root, "_batch_kind", None)
            or controller.sendNUM != 0
            or controller.loadNUM != 0
            or controller.paused
        ):
            raise ApiError(HTTPStatus.CONFLICT, "another file transfer is in progress, try again")

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        root = self._root
        done = threading.Event()
        box: dict[str, Any] = {}

        def on_listing(file_list):
            box["files"] = file_list
            box["error"] = root.quiet_machine_ls_error
            done.set()

        def start():
            self._ensure_free("list files")
            if root.fill_remote_dir_callback is not None:
                raise ApiError(HTTPStatus.CONFLICT, "another file listing is in progress, try again")
            root.fill_remote_dir_callback = on_listing
            root.fill_remote_dir_callback_path = path
            # report a failed listing back here instead of as a popup on the operator's screen
            root.quiet_machine_ls_path = path
            root.quiet_machine_ls_error = None
            root.request_machine_ls(path)

        def release():
            if root.fill_remote_dir_callback is on_listing:
                root.fill_remote_dir_callback = None
                root.fill_remote_dir_callback_path = None
            # leave the quiet path alone if a newer API listing has taken the slot
            if root.fill_remote_dir_callback is None and root.quiet_machine_ls_path == path:
                root.quiet_machine_ls_path = None
                root.quiet_machine_ls_error = None

        self._on_main_thread(start)
        finished = done.wait(LIST_TIMEOUT)
        Clock.schedule_once(lambda _dt: release(), 0)
        if not finished or box.get("error") == "timeout":
            raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, f"machine did not list {path} in time")
        if box.get("error"):
            raise ApiError(HTTPStatus.NOT_FOUND, f"machine could not list {path}")
        return [
            {
                "name": entry["name"],
                "path": entry["path"],
                "is_dir": entry["is_dir"],
                "size": entry["size"],
                "modified": entry.get("date_raw"),
            }
            for entry in box["files"]
        ]

    def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        root = self._root
        controller = self._controller

        # Prepare the local copy before claiming the file channel. Its name is never
        # the remote one, which may not be a valid (or safe) file name on Windows.
        fd, local_path = tempfile.mkstemp(prefix="remote_api_", suffix=".upload", dir=root.temp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            md5 = Utils.md5(local_path)

            def claim():
                self._ensure_free("upload")
                root.uploading = True
                controller.sendNUM = SEND_FILE
                # The modal progress popup shows the operator what is happening and, as
                # for the file browser's own uploads, keeps other transfers from starting.
                root.progressStart(
                    tr._("Uploading") + "\n%s\n" % remote_path + tr._("(Remote API)"), root.cancelProcessingFile
                )

            self._on_main_thread(claim)
            result, error = self._transfer(local_path, md5, remote_path, len(data))
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass

        if not result:
            message = error or "upload failed or was cancelled"
            controller.log.put((Controller.MSG_ERROR, f"Remote API upload of {remote_path} failed: {message}"))
            raise ApiError(HTTPStatus.BAD_GATEWAY, message)

        controller.log.put((Controller.MSG_NORMAL, f"Remote API uploaded {remote_path} ({len(data)} bytes)"))
        Clock.schedule_once(root.file_popup.refresh_machine, 0)
        return {"path": remote_path, "bytes": len(data), "md5": md5}

    def _transfer(self, local_path: str, md5: str, remote_path: str, size: int) -> tuple[Any, str | None]:
        """Send the file once the channel is claimed; always releases the claim. Returns (result, error)."""
        root = self._root
        controller = self._controller

        def progress(packet_size, total_packets, success_count, error_count):
            packets = max(1, -(-size // packet_size))
            Clock.schedule_once(lambda _dt: root.progressUpdate(total_packets * 100.0 / packets, "", False), 0)

        try:
            stream = controller.stream
            if stream is None:
                raise ConnectionError("connection to the machine was lost")
            controller.pauseStream(1)
            try:
                controller.uploadCommand(remote_path)
                result = stream.upload(local_path, md5, progress)
                modem = getattr(stream, "modem", None)
                return result, getattr(modem, "last_file_error", None) if modem else None
            finally:
                controller.resumeStream()
        except Exception as e:
            logger.exception("Remote API: upload failed")
            return False, str(e)
        finally:
            root.uploading = False
            controller.sendNUM = 0
            root.heartbeat_time = time.time()
            root._schedule_progress_finish()
