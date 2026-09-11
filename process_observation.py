#!/usr/bin/env python3
"""AAW V0.4B observed process-start receipts.

This module exists so that lifecycle recording never has to guess that a
process started. It converts a *live* ``subprocess.Popen`` into a receipt of
what was actually observed, and lets a runner scope an observer around an
adapter call it does not control directly.

It deliberately contains no ledger logic, no workflow logic and no provider
logic. It only reports observations.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


# Bounded start-evidence taxonomy. Each value states what was actually seen.
CHILD_PROCESS_SPAWNED = "CHILD_PROCESS_SPAWNED"
HTTP_REQUEST_DISPATCH_INITIATED = "HTTP_REQUEST_DISPATCH_INITIATED"

Observer = Callable[[Mapping[str, Any]], None]

_OBSERVER: ContextVar[Observer | None] = ContextVar("aaw_process_start_observer", default=None)


@contextmanager
def observation_scope(observer: Observer | None) -> Iterator[None]:
    """Route start receipts produced inside this block to ``observer``.

    Used where the runner owns execution identity but the spawn happens deeper
    (for example inside a pluggable Custom Job adapter).
    """
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def current_observer() -> Observer | None:
    return _OBSERVER.get()


def _iso(value: dt.datetime) -> str:
    return value.astimezone().isoformat(timespec="milliseconds")


def _windows_creation_time(popen: subprocess.Popen[Any]) -> str | None:
    handle = getattr(popen, "_handle", None)
    if handle is None:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        ok = kernel32.GetProcessTimes(
            wintypes.HANDLE(int(handle)), ctypes.byref(creation), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        if ticks <= 0:
            return None
        epoch = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
        return _iso(epoch + dt.timedelta(microseconds=ticks / 10))
    except Exception:  # observation is best-effort; never break a dispatch
        return None


def _posix_creation_time(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8", errors="replace")
        fields = raw[raw.rfind(")") + 2:].split()
        start_ticks = int(fields[19])
        hertz = os.sysconf("SC_CLK_TCK")
        boot = None
        for line in Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("btime "):
                boot = int(line.split()[1])
                break
        if boot is None or hertz <= 0:
            return None
        moment = dt.datetime.fromtimestamp(boot + start_ticks / hertz, dt.timezone.utc)
        return _iso(moment)
    except Exception:
        return None


def process_creation_time(popen: subprocess.Popen[Any]) -> str | None:
    """Real OS process creation time when the platform exposes it, else ``None``.

    ``None`` is an honest absence. It is never replaced by the observation
    timestamp, because that would turn a runner clock reading into a claim
    about the process itself.
    """
    if sys.platform == "win32":
        return _windows_creation_time(popen)
    if popen.pid:
        return _posix_creation_time(int(popen.pid))
    return None


def process_receipt(
    popen: subprocess.Popen[Any], *, executable: str | None = None,
    adapter: str | None = None, argv: Sequence[str] | None = None,
    cwd: Path | str | None = None, provider: str | None = None,
) -> dict[str, Any]:
    """Receipt for a child process that AAW itself successfully spawned."""
    created = process_creation_time(popen)
    return {
        "start_evidence": CHILD_PROCESS_SPAWNED,
        "observed_start_time": _iso(dt.datetime.now()),
        "process_id": int(popen.pid) if popen.pid else None,
        "process_creation_time": created,
        "process_creation_time_source": (
            "OS_PROCESS_TIMES" if created else "UNAVAILABLE_NO_PROCESS_OWNERSHIP_API"
        ),
        "process_owner": "AAW_RUNNER_CHILD",
        "executable": str(executable) if executable else (list(argv)[0] if argv else None),
        "provider": provider,
        "adapter": adapter,
        "cwd": str(cwd) if cwd is not None else None,
        "proves": "AAW spawned this child process and received its OS identity.",
        "does_not_prove": "That any provider-side or remote work began, completed or billed.",
    }


def http_receipt(*, endpoint: str, adapter: str, provider: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Receipt for a local HTTP inference call that AAW dispatched itself.

    AAW owns no process here, so the strongest honest boundary is that the
    request was dispatched after a successful availability precheck.
    """
    return {
        "start_evidence": HTTP_REQUEST_DISPATCH_INITIATED,
        "observed_start_time": _iso(dt.datetime.now()),
        "process_id": None,
        "process_creation_time": None,
        "process_creation_time_source": "NOT_APPLICABLE_NO_CHILD_PROCESS",
        "process_owner": "NOT_OWNED_BY_AAW",
        "endpoint": endpoint,
        "provider": provider,
        "model": model,
        "adapter": adapter,
        "proves": "AAW dispatched this request after a successful local availability precheck.",
        "does_not_prove": "That server-side inference began, completed or produced an effect.",
    }


def notify(receipt: Mapping[str, Any]) -> None:
    """Deliver a receipt to the currently scoped observer, if any."""
    observer = _OBSERVER.get()
    if observer is None:
        return
    observer(dict(receipt))


def notify_process_start(popen: subprocess.Popen[Any], *, dispatch: bool = False, **fields: Any) -> None:
    """Report a spawn, but only when it *is* the invocation's dispatch.

    ``dispatch`` must be set explicitly by the call site that launches the
    invocation itself. Incidental helper spawns inside the same observation
    scope — a ``git rev-parse`` an adapter runs while assembling its package,
    say — must never be reported, or the ledger would attribute a helper
    process to the execution as its start evidence.
    """
    if not dispatch or _OBSERVER.get() is None:
        return
    notify(process_receipt(popen, **fields))
