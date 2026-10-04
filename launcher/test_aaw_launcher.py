"""Deterministic tests for the canonical AAW launcher.

Run explicitly (this directory is outside the root `test_*.py` discovery
window, same convention as `CONTROL_CENTER`'s own test suite):

    python -m pytest -q launcher

No real browser is opened and no real process is spawned or killed: every
external boundary (HTTP, subprocess, webbrowser) is monkeypatched.
"""

from __future__ import annotations

import argparse
import json

import pytest

import aaw_launcher as launcher


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Never touch the real repo's .runtime/launcher directory."""
    monkeypatch.setattr(launcher, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(launcher, "STATE_FILE", tmp_path / "runtime" / "state.json")
    monkeypatch.setattr(launcher, "LOG_FILE", tmp_path / "runtime" / "launcher.log")
    monkeypatch.setattr(launcher, "BRIDGE_LOG_FILE", tmp_path / "runtime" / "bridge.log")
    monkeypatch.setattr(launcher, "_open_ui", lambda host, port, open_browser: f"http://{host}:{port}/")
    return tmp_path


def _args(**overrides) -> argparse.Namespace:
    base = dict(host="127.0.0.1", port=8787, repo=None, worktree=None,
                timeout=1.0, open_browser=False)
    base.update(overrides)
    return argparse.Namespace(**base)


class FakeProc:
    def __init__(self, exit_code=None, pid=4242):
        self._exit_code = exit_code
        self.pid = pid

    def poll(self):
        return self._exit_code


# ── check_health ─────────────────────────────────────────────────────────

def test_check_health_healthy_instance(monkeypatch):
    monkeypatch.setattr(launcher, "_tcp_connect_ok", lambda host, port, timeout: True)
    monkeypatch.setattr(launcher, "_http_get_json", lambda url, timeout: {"bridge_version": "X"})
    status, data = launcher.check_health("127.0.0.1", 8787)
    assert status == launcher.Outcome.AAW_HEALTHY
    assert data == {"bridge_version": "X"}


def test_check_health_no_instance_when_nothing_accepts_connections(monkeypatch):
    monkeypatch.setattr(launcher, "_tcp_connect_ok", lambda host, port, timeout: False)

    def _forbid_http(*a, **k):
        raise AssertionError("must not attempt HTTP when nothing is listening")
    monkeypatch.setattr(launcher, "_http_get_json", _forbid_http)

    status, data = launcher.check_health("127.0.0.1", 8787)
    assert status == launcher.Outcome.NO_AAW_INSTANCE
    assert data is None


def test_check_health_occupied_when_listener_does_not_answer_http(monkeypatch):
    monkeypatch.setattr(launcher, "_tcp_connect_ok", lambda host, port, timeout: True)

    def _raise(url, timeout):
        raise TimeoutError("no response")
    monkeypatch.setattr(launcher, "_http_get_json", _raise)
    status, data = launcher.check_health("127.0.0.1", 8787)
    assert status == launcher.Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS


def test_check_health_occupied_when_json_missing_bridge_version(monkeypatch):
    monkeypatch.setattr(launcher, "_tcp_connect_ok", lambda host, port, timeout: True)
    monkeypatch.setattr(launcher, "_http_get_json", lambda url, timeout: {"unrelated": True})
    status, data = launcher.check_health("127.0.0.1", 8787)
    assert status == launcher.Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS


# ── wait_until_ready ─────────────────────────────────────────────────────

def test_wait_until_ready_success(monkeypatch):
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    calls = {"n": 0}

    def fake_health(host, port, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return launcher.Outcome.NO_AAW_INSTANCE, None
        return launcher.Outcome.AAW_HEALTHY, {"bridge_version": "X"}

    monkeypatch.setattr(launcher, "check_health", fake_health)
    outcome, data = launcher.wait_until_ready(FakeProc(exit_code=None), "127.0.0.1", 8787, 5.0)
    assert outcome == launcher.Outcome.READY
    assert data["bridge_version"] == "X"


def test_wait_until_ready_process_exited(monkeypatch):
    monkeypatch.setattr(launcher, "check_health",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not poll health")))
    outcome, data = launcher.wait_until_ready(FakeProc(exit_code=1), "127.0.0.1", 8787, 5.0)
    assert outcome == launcher.Outcome.PROCESS_EXITED
    assert data is None


def test_wait_until_ready_timeout(monkeypatch):
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(launcher, "check_health", lambda *a, **k: (launcher.Outcome.NO_AAW_INSTANCE, None))
    ticks = iter([0.0, 0.1, 0.2, 10.0])
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))
    outcome, data = launcher.wait_until_ready(FakeProc(exit_code=None), "127.0.0.1", 8787, 1.0)
    assert outcome == launcher.Outcome.TIMEOUT
    assert data is None


def test_wait_until_ready_health_error_on_foreign_response(monkeypatch):
    monkeypatch.setattr(launcher, "check_health",
                         lambda *a, **k: (launcher.Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS, None))
    outcome, data = launcher.wait_until_ready(FakeProc(exit_code=None), "127.0.0.1", 8787, 5.0)
    assert outcome == launcher.Outcome.HEALTH_ERROR


# ── cmd_start ────────────────────────────────────────────────────────────

def test_cmd_start_reuses_existing_healthy_instance(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "check_health",
                         lambda *a, **k: (launcher.Outcome.AAW_HEALTHY, {"bridge_version": "X"}))

    def _forbid_spawn(*a, **k):
        raise AssertionError("must not start a duplicate bridge for a healthy instance")
    monkeypatch.setattr(launcher, "spawn_bridge", _forbid_spawn)

    rc = launcher.cmd_start(_args())
    assert rc == 0
    assert "already running" in capsys.readouterr().out


def test_cmd_start_refuses_when_port_occupied_by_unrelated_process(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "check_health",
                         lambda *a, **k: (launcher.Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS, None))
    monkeypatch.setattr(launcher, "find_port_owner", lambda port: {"pid": 555, "name": "unrelated.exe"})

    killed = {"called": False}
    monkeypatch.setattr(launcher.subprocess, "run",
                         lambda *a, **k: killed.__setitem__("called", True))

    def _forbid_spawn(*a, **k):
        raise AssertionError("must not start a bridge when the port is occupied")
    monkeypatch.setattr(launcher, "spawn_bridge", _forbid_spawn)

    rc = launcher.cmd_start(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "555" in out and "unrelated.exe" in out
    assert killed["called"] is False  # never terminate the unrelated process


def test_cmd_start_repository_incomplete(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "check_health", lambda *a, **k: (launcher.Outcome.NO_AAW_INSTANCE, None))
    monkeypatch.setattr(launcher, "BRIDGE_SERVER", tmp_path / "missing_bridge.py")

    def _forbid_spawn(*a, **k):
        raise AssertionError("must not spawn a bridge that does not exist")
    monkeypatch.setattr(launcher, "spawn_bridge", _forbid_spawn)

    rc = launcher.cmd_start(_args())
    assert rc == 1
    assert "incomplete" in capsys.readouterr().out


def test_cmd_start_bridge_startup_success_writes_state(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "check_health", lambda *a, **k: (launcher.Outcome.NO_AAW_INSTANCE, None))
    monkeypatch.setattr(launcher, "BRIDGE_SERVER", tmp_path / "aaw_bridge_server.py")
    (tmp_path / "aaw_bridge_server.py").write_text("# stub", encoding="utf-8")
    fake_proc = FakeProc(exit_code=None, pid=9001)
    monkeypatch.setattr(launcher, "spawn_bridge", lambda *a, **k: fake_proc)
    monkeypatch.setattr(launcher, "wait_until_ready",
                         lambda *a, **k: (launcher.Outcome.READY, {"bridge_version": "X"}))

    rc = launcher.cmd_start(_args())
    assert rc == 0
    state = json.loads(launcher.STATE_FILE.read_text(encoding="utf-8"))
    assert state["pid"] == 9001
    assert state["started_by_launcher"] is True


def test_cmd_start_bridge_process_exited(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "check_health", lambda *a, **k: (launcher.Outcome.NO_AAW_INSTANCE, None))
    monkeypatch.setattr(launcher, "BRIDGE_SERVER", tmp_path / "aaw_bridge_server.py")
    (tmp_path / "aaw_bridge_server.py").write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(launcher, "spawn_bridge", lambda *a, **k: FakeProc(exit_code=1))
    monkeypatch.setattr(launcher, "wait_until_ready", lambda *a, **k: (launcher.Outcome.PROCESS_EXITED, None))

    rc = launcher.cmd_start(_args())
    assert rc == 1
    assert "exited before becoming ready" in capsys.readouterr().out
    assert not launcher.STATE_FILE.exists()


def test_cmd_start_health_timeout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "check_health", lambda *a, **k: (launcher.Outcome.NO_AAW_INSTANCE, None))
    monkeypatch.setattr(launcher, "BRIDGE_SERVER", tmp_path / "aaw_bridge_server.py")
    (tmp_path / "aaw_bridge_server.py").write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(launcher, "spawn_bridge", lambda *a, **k: FakeProc(exit_code=None, pid=77))
    monkeypatch.setattr(launcher, "wait_until_ready", lambda *a, **k: (launcher.Outcome.TIMEOUT, None))

    rc = launcher.cmd_start(_args())
    assert rc == 1
    assert "did not become healthy" in capsys.readouterr().out
    assert not launcher.STATE_FILE.exists()


def test_cmd_start_spawn_raises_oserror(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "check_health", lambda *a, **k: (launcher.Outcome.NO_AAW_INSTANCE, None))
    monkeypatch.setattr(launcher, "BRIDGE_SERVER", tmp_path / "aaw_bridge_server.py")
    (tmp_path / "aaw_bridge_server.py").write_text("# stub", encoding="utf-8")

    def _raise(*a, **k):
        raise FileNotFoundError("python executable not found")
    monkeypatch.setattr(launcher, "spawn_bridge", _raise)

    rc = launcher.cmd_start(_args())
    assert rc == 1
    assert "Failed to launch the bridge process" in capsys.readouterr().out


# ── cmd_stop ─────────────────────────────────────────────────────────────

def test_cmd_stop_no_tracked_instance(capsys):
    rc = launcher.cmd_stop(_args())
    assert rc == 0
    assert "No launcher-tracked" in capsys.readouterr().out


def test_cmd_stop_cleans_stale_metadata_when_process_gone(monkeypatch):
    launcher.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    launcher.STATE_FILE.write_text(json.dumps({"pid": 123}), encoding="utf-8")
    monkeypatch.setattr(launcher, "is_process_alive", lambda pid: False)

    rc = launcher.cmd_stop(_args())
    assert rc == 0
    assert not launcher.STATE_FILE.exists()


def test_cmd_stop_refuses_when_process_identity_does_not_match(monkeypatch):
    launcher.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    launcher.STATE_FILE.write_text(json.dumps({"pid": 123}), encoding="utf-8")
    monkeypatch.setattr(launcher, "is_process_alive", lambda pid: True)
    monkeypatch.setattr(launcher, "_process_commandline", lambda pid: "C:\\Windows\\notepad.exe")

    killed = {"called": False}
    monkeypatch.setattr(launcher.subprocess, "run",
                         lambda *a, **k: killed.__setitem__("called", True))

    rc = launcher.cmd_stop(_args())
    assert rc == 1
    assert killed["called"] is False
    assert launcher.STATE_FILE.exists()  # metadata preserved; nothing was proven safe to clear


def test_cmd_stop_kills_verified_bridge_process(monkeypatch):
    launcher.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    launcher.STATE_FILE.write_text(json.dumps({"pid": 123}), encoding="utf-8")
    monkeypatch.setattr(launcher, "is_process_alive", lambda pid: True)
    monkeypatch.setattr(launcher, "_process_commandline",
                         lambda pid: "C:\\Python314\\python.exe aaw_bridge_server.py --port 8787")
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: None)

    rc = launcher.cmd_stop(_args())
    assert rc == 0
    assert not launcher.STATE_FILE.exists()


# ── CLI wiring ───────────────────────────────────────────────────────────

def test_main_defaults_to_start(monkeypatch):
    seen = {}

    def _fake_start(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(launcher, "cmd_start", _fake_start)
    rc = launcher.main([])
    assert rc == 0
    assert seen["args"].host == launcher.DEFAULT_HOST


def test_main_dispatches_stop(monkeypatch):
    monkeypatch.setattr(launcher, "cmd_stop", lambda args: 0)
    assert launcher.main(["stop"]) == 0
