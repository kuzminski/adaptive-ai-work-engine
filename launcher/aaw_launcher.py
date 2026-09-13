#!/usr/bin/env python3
"""AAW canonical launcher — orchestration glue around the existing bridge server.

This module starts no new server and holds no workflow knowledge of its own.
It answers one question ("is the current AAW bridge already serving the live
canvas, and if not, can we start it safely?") and then gets out of the way by
opening the browser at the bridge's own URL.

Standard library only, matching `aaw_bridge_server.py`'s own zero-dependency
posture. Windows process/port ownership checks shell out to PowerShell
(`Get-NetTCPConnection`, `Get-CimInstance Win32_Process`), which ships with
Windows 10/11 — this avoids a `psutil` dependency for the few things it would
have bought us here.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

LAUNCHER_VERSION = "0.1"

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_SERVER = REPO_ROOT / "aaw_bridge_server.py"
RUNTIME_DIR = REPO_ROOT / ".runtime" / "launcher"
STATE_FILE = RUNTIME_DIR / "state.json"
LOG_FILE = RUNTIME_DIR / "launcher.log"
BRIDGE_LOG_FILE = RUNTIME_DIR / "bridge.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787  # must track aaw_bridge_server.DEFAULT_PORT
DEFAULT_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.3
HEALTH_TIMEOUT_SECONDS = 1.5
MAX_LOG_BYTES = 1_000_000


class Outcome:
    NO_AAW_INSTANCE = "NO_AAW_INSTANCE"
    AAW_HEALTHY = "AAW_HEALTHY"
    PORT_OCCUPIED_BY_OTHER_PROCESS = "PORT_OCCUPIED_BY_OTHER_PROCESS"
    READY = "READY"
    TIMEOUT = "TIMEOUT"
    PROCESS_EXITED = "PROCESS_EXITED"
    HEALTH_ERROR = "HEALTH_ERROR"


# ── logging ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _rotate_log_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
    except OSError:
        pass


def log(line: str) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_needed(LOG_FILE)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} {line}\n")
    except OSError:
        pass  # a launcher must never fail because logging failed


def _fail_readable(title: str, detail: str, hint: str) -> None:
    log(f"FAILURE: {title} | {detail}")
    print()
    print(title)
    print()
    print(detail)
    print()
    print(hint)
    print()


# ── health ───────────────────────────────────────────────────────────────

def _http_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _tcp_connect_ok(host: str, port: int, timeout: float) -> bool:
    """Is anything at all accepting TCP connections on this port?

    Deliberately does not try to distinguish "refused" from "timed out": on
    some machines a closed loopback port is silently dropped by local
    firewall/security software instead of RST'd, so a connect timeout means
    the same thing a refusal does here — nobody is listening.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_health(host: str, port: int, timeout: float = HEALTH_TIMEOUT_SECONDS) -> tuple[str, dict | None]:
    """Distinguish nobody-listening from something-listening-that-is-not-AAW.

    First ask whether anything is even accepting connections on the port. If
    not, the port is free. If something is listening, only *then* is an HTTP
    round trip meaningful: a timeout or bad response at that point means
    something else is bound to the port, not that AAW is merely absent.
    """
    if not _tcp_connect_ok(host, port, timeout):
        return Outcome.NO_AAW_INSTANCE, None
    url = f"http://{host}:{port}/api/contract"
    try:
        data = _http_get_json(url, timeout)
    except Exception:  # noqa: BLE001 - any failure here means "not our bridge"
        return Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS, None
    if isinstance(data, dict) and data.get("bridge_version"):
        return Outcome.AAW_HEALTHY, data
    return Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS, data


# ── Windows process/port inspection (read-only; never kills unrelated processes) ─

def find_port_owner(port: int) -> dict | None:
    """Best-effort PID + process name currently listening on `port`."""
    script = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($c) { $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{pid=$c.OwningProcess; name=$p.ProcessName} | ConvertTo-Json -Compress }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=5,
        )
        text = out.stdout.strip()
        if not text:
            return None
        info = json.loads(text)
        return {"pid": info.get("pid"), "name": info.get("name")}
    except Exception:  # noqa: BLE001 - diagnostics only, never fatal
        return None


def is_process_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'true' }} else {{ 'false' }}"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip().lower() == "true"
    except Exception:  # noqa: BLE001
        return False


def _process_commandline(pid: int) -> str | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
             "-ErrorAction SilentlyContinue).CommandLine"],
            capture_output=True, text=True, timeout=5,
        )
        text = out.stdout.strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None


# ── bridge lifecycle ─────────────────────────────────────────────────────

def spawn_bridge(python_exe: str, host: str, port: int, repo: Path | None,
                  worktree: Path | None, log_path: Path) -> subprocess.Popen:
    # -u: unbuffered stdio, so the log file reflects bridge output immediately
    # instead of sitting in a block buffer until enough of it accumulates.
    argv = [python_exe, "-u", str(BRIDGE_SERVER), "--host", host, "--port", str(port)]
    if repo and worktree:
        argv += ["--repo", str(repo), "--worktree", str(worktree)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log_if_needed(log_path)
    log_fh = open(log_path, "ab")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    log(f"spawning bridge: {' '.join(argv)}")
    return subprocess.Popen(
        argv, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
        stdout=log_fh, stderr=log_fh, creationflags=creationflags,
    )


def wait_until_ready(proc: subprocess.Popen, host: str, port: int,
                      timeout_seconds: float) -> tuple[str, dict | None]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if proc.poll() is not None:
            return Outcome.PROCESS_EXITED, None
        status, data = check_health(host, port)
        if status == Outcome.AAW_HEALTHY:
            return Outcome.READY, data
        if status == Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS:
            # Our own freshly-spawned process answering as "not AAW" would be
            # a bug in the bridge itself, not a startup race.
            return Outcome.HEALTH_ERROR, data
        if time.monotonic() >= deadline:
            return Outcome.TIMEOUT, None
        time.sleep(POLL_INTERVAL_SECONDS)


def _write_state(pid: int, host: str, port: int) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "pid": pid, "host": host, "port": port,
        "started_by_launcher": True, "repo_root": str(REPO_ROOT),
        "started_at": _now_iso(),
    }, indent=2), encoding="utf-8")


def _open_ui(host: str, port: int, open_browser: bool) -> str:
    url = f"http://{host}:{port}/"
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - never fail startup over a browser launch
            log("failed to open the default browser; the UI is still reachable at " + url)
    return url


# ── commands ─────────────────────────────────────────────────────────────

def cmd_start(args: argparse.Namespace) -> int:
    host, port = args.host, args.port
    log(f"launcher v{LAUNCHER_VERSION} start requested; python={sys.executable} host={host} port={port}")

    status, contract = check_health(host, port)
    if status == Outcome.AAW_HEALTHY:
        log(f"reusing existing healthy instance (bridge_version={contract.get('bridge_version')})")
        url = _open_ui(host, port, args.open_browser)
        print(f"AAW is already running. Opening {url}")
        return 0

    if status == Outcome.PORT_OCCUPIED_BY_OTHER_PROCESS:
        owner = find_port_owner(port)
        owned_by = f" (PID {owner['pid']}, {owner['name']})" if owner and owner.get("pid") else ""
        _fail_readable(
            "AAW could not start.",
            f"Port {port} is already occupied by another process{owned_by}, "
            "and it did not answer as an AAW bridge.",
            "Stop that process yourself if it is safe to do so, or start AAW on a different "
            f"port with --port <port>. The other process was left running.",
        )
        return 1

    if not BRIDGE_SERVER.exists():
        _fail_readable(
            "AAW could not start.",
            f"Expected bridge server not found: {BRIDGE_SERVER}",
            "This repository checkout looks incomplete.",
        )
        return 1

    try:
        proc = spawn_bridge(sys.executable, host, port, args.repo, args.worktree, BRIDGE_LOG_FILE)
    except OSError as exc:
        _fail_readable("AAW could not start.", f"Failed to launch the bridge process: {exc}",
                        f"Log: {LOG_FILE}")
        return 1

    log(f"bridge process started: pid={proc.pid}")
    outcome, contract = wait_until_ready(proc, host, port, args.timeout)

    if outcome == Outcome.READY:
        _write_state(proc.pid, host, port)
        log(f"bridge ready: pid={proc.pid} bridge_version={contract.get('bridge_version')}")
        url = _open_ui(host, port, args.open_browser)
        print(f"AAW is ready. Opening {url}")
        return 0

    if outcome == Outcome.PROCESS_EXITED:
        _fail_readable(
            "AAW failed to start.",
            "Bridge process exited before becoming ready.",
            f"Log: {BRIDGE_LOG_FILE}",
        )
        return 1

    if outcome == Outcome.TIMEOUT:
        _fail_readable(
            "AAW failed to start.",
            f"Bridge did not become healthy within {args.timeout:.0f}s.",
            f"Bridge process (pid={proc.pid}) is still running; it was left alone. "
            f"Check the log: {BRIDGE_LOG_FILE}",
        )
        return 1

    _fail_readable(
        "AAW failed to start.",
        "Bridge responded, but not as a recognizable AAW instance.",
        f"Log: {BRIDGE_LOG_FILE}",
    )
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    if not STATE_FILE.exists():
        print("No launcher-tracked AAW instance found.")
        return 0
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("Runtime metadata is unreadable; not stopping anything.")
        return 1
    pid = state.get("pid")
    if not isinstance(pid, int):
        print("Runtime metadata is invalid; not stopping anything.")
        return 1
    if not is_process_alive(pid):
        print(f"Process {pid} is no longer running. Clearing stale runtime metadata.")
        STATE_FILE.unlink(missing_ok=True)
        return 0
    cmdline = _process_commandline(pid) or ""
    if "aaw_bridge_server.py" not in cmdline:
        print(f"Process {pid} no longer looks like the AAW bridge (command line does not match).")
        print("Refusing to stop it. Stop it yourself if you are sure, e.g. via Task Manager.")
        return 1
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Stop-Process -Id {pid} -Force"],
            capture_output=True, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to stop process {pid}: {exc}")
        return 1
    log(f"stopped bridge pid={pid}")
    STATE_FILE.unlink(missing_ok=True)
    print(f"Stopped AAW (pid {pid}).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    status, contract = check_health(args.host, args.port)
    print(status)
    if contract:
        print(json.dumps(contract, indent=2, ensure_ascii=False)[:2000])
    return 0 if status == Outcome.AAW_HEALTHY else 1


# ── CLI ──────────────────────────────────────────────────────────────────

def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--repo", type=Path, default=None,
                   help="canonical repository a run may read (forwarded to the bridge)")
    p.add_argument("--worktree", type=Path, default=None,
                   help="isolated worktree a run may write (forwarded to the bridge)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="AAW canonical launcher")
    sub = ap.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="start or reuse AAW and open the live canvas (default)")
    _add_common_args(p_start)
    p_start.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                          help="seconds to wait for the bridge to become healthy")
    p_start.add_argument("--no-browser", dest="open_browser", action="store_false",
                          help="do not open a browser tab")
    p_start.set_defaults(open_browser=True, func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop a launcher-started AAW instance")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="report AAW health without starting or opening anything")
    _add_common_args(p_status)
    p_status.set_defaults(func=cmd_status)

    return ap


_COMMANDS = ("start", "stop", "status")


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    if not raw or raw[0] not in _COMMANDS:
        raw = ["start", *raw]
    args = build_parser().parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
