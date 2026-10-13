"""Tests for starting and stopping the Remote API from settings."""

from __future__ import annotations

import http.client
import queue
import types

import pytest
from kivy.config import Config

from carveracontroller.addons.remote_api import service as service_mod
from carveracontroller.addons.remote_api.service import RemoteApiService

KEYS = ("remote_api_enabled", "remote_api_host", "remote_api_port", "remote_api_token", "remote_api_allow_upload")


@pytest.fixture
def settings(monkeypatch):
    if not Config.has_section("carvera"):
        Config.add_section("carvera")
    saved = {k: Config.get("carvera", k) for k in KEYS if Config.has_option("carvera", k)}
    monkeypatch.setattr(Config, "write", lambda: None)

    def apply(**values):
        for key, value in values.items():
            Config.set("carvera", key, value)

    apply(
        remote_api_enabled="1",
        remote_api_host="127.0.0.1",
        remote_api_port="0",
        remote_api_token="",
        remote_api_allow_upload="0",
    )
    yield apply
    for k in KEYS:
        if k in saved:
            Config.set("carvera", k, saved[k])
        elif Config.has_option("carvera", k):
            Config.remove_option("carvera", k)


@pytest.fixture
def svc():
    root = types.SimpleNamespace(controller=types.SimpleNamespace(log=queue.Queue()))
    service = RemoteApiService(root, "9.9.9")
    yield service, root
    service.stop()


def health(service):
    host, port = service._server.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/api/v1/health")
    status = conn.getresponse().status
    conn.close()
    return status


def test_disabled_by_setting(settings, svc):
    service, _ = svc
    settings(remote_api_enabled="0")
    service.apply_config()
    assert not service.running


def test_starts_and_generates_token(settings, svc):
    service, _ = svc
    service.apply_config()
    assert service.running
    assert health(service) == 200
    token = Config.get("carvera", "remote_api_token")
    assert len(token) >= 24
    # re-applying keeps the token and restarts cleanly
    service.apply_config()
    assert service.running
    assert Config.get("carvera", "remote_api_token") == token
    service.stop()
    assert not service.running


def test_bind_failure_is_reported_not_raised(settings, svc, monkeypatch):
    service, root = svc

    def fail(self):
        raise OSError("address in use")

    monkeypatch.setattr(service_mod.RemoteApiServer, "start", fail)
    service.apply_config()
    assert not service.running
    assert "address in use" in root.controller.log.get_nowait()[1]


@pytest.mark.parametrize("port", ["70000", "-1", "abc"])
def test_bad_port_is_reported_not_raised(settings, svc, port):
    service, root = svc
    settings(remote_api_port=port)
    service.apply_config()
    assert not service.running
    assert "port" in root.controller.log.get_nowait()[1]


def test_generated_token_is_shown_in_settings(settings, svc):
    service, root = svc
    shown = []
    root.config_popup = types.SimpleNamespace(show_value=lambda *args: shown.append(args))
    service.apply_config()
    assert shown == [("carvera", "remote_api_token", Config.get("carvera", "remote_api_token"))]


def test_not_started_on_mobile(settings, svc, monkeypatch):
    service, _ = svc
    monkeypatch.setattr(service_mod, "kivy_platform", "android")
    service.apply_config()
    assert not service.running
