"""Tests for the Remote API backend against fake app, root and controller objects."""

from __future__ import annotations

import hashlib
import queue
import types
from pathlib import Path

import pytest

from carveracontroller.addons.remote_api import backend as backend_mod
from carveracontroller.addons.remote_api.backend import KivyMachineBackend
from carveracontroller.addons.remote_api.console import ConsoleBuffer
from carveracontroller.addons.remote_api.server import ApiError
from carveracontroller.Controller import CONN_WIFI, SEND_FILE


class ImmediateClock:
    """Runs scheduled callbacks at once, standing in for the Kivy main loop."""

    @staticmethod
    def schedule_once(callback, timeout=0):
        callback(0)


class FakeStream:
    def __init__(self, result=True):
        self.result = result
        self.uploads = []
        self.modem = types.SimpleNamespace(last_file_error=None)

    def upload(self, filename, md5, callback):
        with open(filename, "rb") as f:
            self.uploads.append((f.read(), md5))
        callback(8192, 1, 1, 0)
        return self.result


class FakeController:
    MSG_NORMAL = 0
    MSG_ERROR = 1

    def __init__(self, stream):
        self.stream = stream
        self.connection_type = CONN_WIFI
        self.sendNUM = 0
        self.loadNUM = 0
        self.paused = False
        self.log = queue.Queue()
        self.commands = []

    def pauseStream(self, wait_s=0.0):
        self.commands.append("pause")
        self.paused = True

    def resumeStream(self):
        self.commands.append("resume")
        self.paused = False

    def uploadCommand(self, filename):
        self.commands.append(("upload", filename, self.sendNUM))


class FakeRoot:
    def __init__(self, controller, temp_dir):
        self.controller = controller
        self.temp_dir = str(temp_dir)
        self.uploading = False
        self.downloading = False
        self.downloading_config = False
        self.heartbeat_time = 0
        self.config_loaded = True
        self.setting_list = {"spindle.max_rpm": 13000}
        self.fw_version = "1.1.2"
        self.fill_remote_dir_callback = None
        self.fill_remote_dir_callback_path = None
        self.quiet_machine_ls_path = None
        self.quiet_machine_ls_error = None
        self.ls_error = None  # what the fake machine reports for the next listing
        self.backing_up_config = False
        self.decompstatus = False
        self.ls_requests = []
        self.listing = [
            {"name": "a.nc", "path": "/sd/gcodes/a.nc", "is_dir": False, "size": 3, "date": 0, "date_raw": "2026"}
        ]
        self.file_popup = types.SimpleNamespace(refresh_machine=lambda *_args: None)
        self._batch_kind = None
        self.progress = []

    def progressStart(self, text, cancel_func, *args):
        self.progress.append(("start", text))

    def progressUpdate(self, value, progress_text, button_disabled, *args):
        self.progress.append(("update", value))

    def _schedule_progress_finish(self, delay=0):
        self.progress.append(("finish",))

    def cancelProcessingFile(self):
        pass

    def request_machine_ls(self, path):
        self.ls_requests.append(path)
        if self.ls_error and self.quiet_machine_ls_path == path:
            self.quiet_machine_ls_error = self.ls_error
        callback = self.fill_remote_dir_callback
        if callback is not None and self.fill_remote_dir_callback_path == path:
            self.fill_remote_dir_callback = None
            self.fill_remote_dir_callback_path = None
            callback(self.listing)


@pytest.fixture
def env(monkeypatch, tmp_path):
    app = types.SimpleNamespace(
        state="Idle", playing=False, model="Z1", is_community_firmware=False, selected_remote_filename=""
    )
    monkeypatch.setattr(backend_mod, "Clock", ImmediateClock)
    monkeypatch.setattr(backend_mod.App, "get_running_app", staticmethod(lambda: app))
    stream = FakeStream()
    controller = FakeController(stream)
    root = FakeRoot(controller, tmp_path)
    return types.SimpleNamespace(app=app, stream=stream, controller=controller, root=root)


def make_backend(env):
    return KivyMachineBackend(env.root, ConsoleBuffer())


def test_upload_success(env):
    data = b"G0 X1\n"
    result = make_backend(env).upload(data, "/sd/gcodes/job.nc")
    md5 = hashlib.md5(data).hexdigest()
    assert result == {"path": "/sd/gcodes/job.nc", "bytes": len(data), "md5": md5}
    assert env.stream.uploads == [(data, md5)]
    assert env.controller.commands == ["pause", ("upload", "/sd/gcodes/job.nc", SEND_FILE), "resume"]
    # transfer state is released and the temp copy removed
    assert env.root.uploading is False and env.controller.sendNUM == 0
    assert list(Path(env.root.temp_dir).iterdir()) == []
    assert "uploaded /sd/gcodes/job.nc" in env.controller.log.get_nowait()[1]
    # the operator sees the standard progress popup, which is closed afterwards
    assert env.root.progress[0][0] == "start" and "/sd/gcodes/job.nc" in env.root.progress[0][1]
    assert ("update", 100.0) in env.root.progress
    assert env.root.progress[-1] == ("finish",)


def test_upload_failure_releases_state(env):
    env.stream.result = False
    env.stream.modem.last_file_error = "SD card full"
    with pytest.raises(ApiError) as exc:
        make_backend(env).upload(b"G0\n", "/sd/gcodes/job.nc")
    assert exc.value.status == 502
    assert "SD card full" in exc.value.message
    assert env.root.uploading is False and env.controller.sendNUM == 0 and env.controller.paused is False
    assert env.root.progress[-1] == ("finish",)


@pytest.mark.parametrize(
    "setup",
    [
        lambda e: setattr(e.app, "state", "Run"),
        lambda e: setattr(e.app, "playing", True),
        lambda e: setattr(e.root, "uploading", True),
        lambda e: setattr(e.root, "downloading_config", True),
        lambda e: setattr(e.root, "_batch_kind", "upload"),
        lambda e: setattr(e.root, "backing_up_config", True),
        lambda e: setattr(e.root, "decompstatus", True),
        lambda e: setattr(e.controller, "loadNUM", 1),
        lambda e: setattr(e.app, "state", "N/A"),
        lambda e: setattr(e.controller, "stream", None),
    ],
)
def test_upload_refused_unless_idle_and_free(env, setup):
    setup(env)
    with pytest.raises(ApiError) as exc:
        make_backend(env).upload(b"G0\n", "/sd/gcodes/job.nc")
    assert exc.value.status == 409
    assert env.stream.uploads == []
    assert env.controller.commands == []
    assert env.root.progress == []


def test_list_dir(env):
    files = make_backend(env).list_dir("/sd/gcodes")
    assert env.root.ls_requests == ["/sd/gcodes"]
    assert files == [{"name": "a.nc", "path": "/sd/gcodes/a.nc", "is_dir": False, "size": 3, "modified": "2026"}]


@pytest.mark.parametrize(
    "setup",
    [
        lambda e: setattr(e.app, "state", "Run"),
        lambda e: setattr(e.root, "uploading", True),
        lambda e: setattr(e.root, "decompstatus", True),
        lambda e: setattr(e.controller, "sendNUM", SEND_FILE),
    ],
)
def test_list_dir_refused_while_busy(env, setup):
    setup(env)
    with pytest.raises(ApiError) as exc:
        make_backend(env).list_dir("/sd/gcodes")
    assert exc.value.status == 409
    assert env.root.ls_requests == []


@pytest.mark.parametrize(("error", "status"), [("error", 404), ("timeout", 504)])
def test_list_dir_failure_is_reported_quietly(env, error, status):
    env.root.ls_error = error
    with pytest.raises(ApiError) as exc:
        make_backend(env).list_dir("/sd/nope")
    assert exc.value.status == status
    # the quiet-listing marker is released again
    assert env.root.quiet_machine_ls_path is None and env.root.quiet_machine_ls_error is None


def test_upload_temp_file_does_not_use_remote_name(env):
    make_backend(env).upload(b"G0\n", "/sd/gcodes/con.nc")
    assert env.stream.uploads == [(b"G0\n", hashlib.md5(b"G0\n").hexdigest())]
    assert list(Path(env.root.temp_dir).iterdir()) == []


def test_list_dir_busy(env):
    env.root.fill_remote_dir_callback = object()
    with pytest.raises(ApiError) as exc:
        make_backend(env).list_dir("/sd/gcodes")
    assert exc.value.status == 409
    assert env.root.ls_requests == []


def test_list_dir_timeout_releases_callback(env, monkeypatch):
    monkeypatch.setattr(backend_mod, "LIST_TIMEOUT", 0.01)
    env.root.request_machine_ls = lambda path: None  # machine never answers
    with pytest.raises(ApiError) as exc:
        make_backend(env).list_dir("/sd/gcodes")
    assert exc.value.status == 504
    assert env.root.fill_remote_dir_callback is None


def test_status(env):
    status = make_backend(env).status()
    assert status["connected"] is True
    assert status["connection"] == "wifi"
    assert status["machine"]["model"] == "Z1"
    assert status["state"] == "Idle"
    assert set(status["position"]["machine"]) == {"x", "y", "z", "a"}


def test_machine_config(env):
    assert make_backend(env).machine_config() == {"spindle.max_rpm": "13000"}
    env.root.config_loaded = False
    assert make_backend(env).machine_config() is None


def test_main_thread_timeout_cancels_the_claim(env, monkeypatch):
    queued = []
    monkeypatch.setattr(backend_mod.Clock, "schedule_once", lambda callback, timeout=0: queued.append(callback))
    monkeypatch.setattr(backend_mod, "MAIN_THREAD_TIMEOUT", 0.01)
    with pytest.raises(ApiError) as exc:
        make_backend(env).upload(b"G0\n", "/sd/gcodes/job.nc")
    assert exc.value.status == 503
    # the main thread gets to the queued claim late: it must not take the transfer
    for callback in queued:
        callback(0)
    assert env.root.uploading is False and env.controller.sendNUM == 0
    assert env.root.progress == []
