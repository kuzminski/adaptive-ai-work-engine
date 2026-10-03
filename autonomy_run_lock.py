#!/usr/bin/env python3
"""AAW AUTONOMOUS ITERATIONS V0.2 — one controller per run.

Two `autonomy_controller` processes must never drive the same run. The lock
is a single file, `<STATS_ROOT>/<run_id>/AUTONOMY/controller.lock`, published
atomically with `os.link` (fails if the name exists, on NTFS and POSIX alike),
so exactly one creator wins. It carries owner evidence:

  * host name, PID **and the OS process creation time** — a PID alone is not
    identity, because PIDs are reused;
  * a random `owner_token`, so two controllers inside one live process are
    still distinguishable.

Classification of an existing lock (never a takeover):

  RUN_LOCK_BUSY                           owner is provably alive, or liveness
                                          cannot be proven either way
                                          (ambiguous ⇒ busy, fail closed)
  RUN_LOCK_STALE_REQUIRES_RECONCILIATION  owner provably dead on this host
                                          (PID gone, or PID reused by a process
                                          with a different creation time)

A stale lock is *reported*, not stolen. `reconcile_stale_lock` is an explicit,
operator-initiated action that re-proves staleness against the exact
`owner_token` the operator inspected and then moves the lock aside as an
immutable record. Local Windows-desktop scope; no distributed locking.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

import process_observation

SCHEMA_VERSION = "AAW_AUTONOMY_RUN_LOCK_V0.2"
LOCK_FILENAME = "controller.lock"

RUN_LOCK_ACQUIRED = "RUN_LOCK_ACQUIRED"
RUN_LOCK_BUSY = "RUN_LOCK_BUSY"
RUN_LOCK_STALE = "RUN_LOCK_STALE_REQUIRES_RECONCILIATION"

# Liveness verdicts for the recorded owner.
OWNER_ALIVE = "ALIVE"
OWNER_DEAD = "DEAD"
OWNER_PID_REUSED = "PID_REUSED"
OWNER_UNKNOWN = "UNKNOWN"


class RunLockError(RuntimeError):
    """The run is not ours to control. `outcome` is BUSY or STALE."""

    def __init__(self, outcome: str, message: str, *, owner: Mapping[str, Any] | None = None,
                 liveness: str | None = None) -> None:
        super().__init__(f"{outcome}: {message}")
        self.outcome, self.owner, self.liveness = outcome, dict(owner or {}), liveness


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


# ── process identity (PID + creation time) ──────────────────────────────────

def _windows_process(pid: int) -> tuple[bool | None, str | None]:
    """(alive, creation_time) via OpenProcess/GetProcessTimes; (None, None) if unknowable."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # 87 = ERROR_INVALID_PARAMETER: no such process. Anything else
            # (e.g. access denied) proves nothing.
            return (False, None) if ctypes.get_last_error() == 87 else (None, None)
        try:
            code = wt.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None, None
            alive = code.value == 259  # STILL_ACTIVE
            c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                return alive, None
            ticks = (int(c.hi) << 32) | int(c.lo)
            epoch = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
            created = (epoch + dt.timedelta(microseconds=ticks / 10)).astimezone().isoformat(timespec="milliseconds")
            return alive, created
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None, None


def _posix_alive(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return None


def process_identity(pid: int) -> tuple[bool | None, str | None]:
    """(alive, OS creation time). `None` means the platform could not tell."""
    if sys.platform == "win32":
        return _windows_process(pid)
    alive = _posix_alive(pid)
    if alive is False:
        return False, None
    # Zombies still answer kill(0); /proc state 'Z' means the process is gone.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        if stat[stat.rfind(")") + 2:].split()[0] == "Z":
            return False, None
    except OSError:
        pass
    return alive, process_observation._posix_creation_time(pid)


def current_owner() -> dict[str, Any]:
    pid = os.getpid()
    _, created = process_identity(pid)
    return {"host": socket.gethostname(), "pid": pid, "process_creation_time": created,
            "process_creation_time_source": "OS_PROCESS_TIMES" if created else "UNAVAILABLE",
            "argv0": Path(sys.argv[0]).name if sys.argv and sys.argv[0] else None}


def owner_liveness(owner: Mapping[str, Any]) -> str:
    """Decide what can be *proven* about a recorded owner. Unknown is never dead."""
    if owner.get("host") != socket.gethostname():
        return OWNER_UNKNOWN  # another machine: nothing local proves anything
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return OWNER_UNKNOWN
    alive, created = process_identity(pid)
    if alive is False:
        return OWNER_DEAD
    if alive is None:
        return OWNER_UNKNOWN
    recorded = owner.get("process_creation_time")
    if recorded and created:
        return OWNER_ALIVE if _same_instant(recorded, created) else OWNER_PID_REUSED
    # PID is alive but its creation time cannot be compared: it may be the
    # owner or a reuse. Ambiguous — treat as alive (busy), never as stale.
    return OWNER_UNKNOWN


def _same_instant(a: str, b: str) -> bool:
    try:
        delta = abs(dt.datetime.fromisoformat(a) - dt.datetime.fromisoformat(b))
    except ValueError:
        return a == b
    return delta <= dt.timedelta(seconds=1)  # /proc start time has clock-tick resolution


# ── the lock ─────────────────────────────────────────────────────────────────

def lock_path(run_dir: Path) -> Path:
    return Path(run_dir) / LOCK_FILENAME


def read_lock(run_dir: Path) -> dict[str, Any] | None:
    path = lock_path(run_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {"unreadable": True}


def inspect_lock(run_dir: Path) -> dict[str, Any]:
    """Read-only classification of whoever holds the run, if anyone."""
    record = read_lock(run_dir)
    if record is None:
        return {"held": False, "outcome": None, "owner": None, "liveness": None}
    if record.get("unreadable") or not isinstance(record.get("owner"), dict):
        return {"held": True, "outcome": RUN_LOCK_BUSY, "owner": record, "liveness": OWNER_UNKNOWN}
    liveness = owner_liveness(record["owner"])
    outcome = RUN_LOCK_STALE if liveness in (OWNER_DEAD, OWNER_PID_REUSED) else RUN_LOCK_BUSY
    return {"held": True, "outcome": outcome, "owner": record, "liveness": liveness}


class RunLock:
    """Exclusive controller ownership of one run. Acquire, use, release."""

    def __init__(self, run_dir: Path, run_id: str, *, purpose: str = "CONTROL") -> None:
        self.run_dir, self.run_id, self.purpose = Path(run_dir), run_id, purpose
        self.path = lock_path(self.run_dir)
        self.record: dict[str, Any] | None = None

    @property
    def held(self) -> bool:
        return self.record is not None

    def acquire(self) -> dict[str, Any]:
        """Return the lock record on RUN_LOCK_ACQUIRED, else raise RunLockError."""
        if self.record is not None:
            return self.record
        self.run_dir.mkdir(parents=True, exist_ok=True)
        record = {"schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                  "owner_token": "LCK_" + uuid.uuid4().hex, "owner": current_owner(),
                  "purpose": self.purpose, "acquired_at": _now()}
        temp = self.path.with_name(f"{LOCK_FILENAME}.{record['owner_token']}.tmp")
        temp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        try:
            os.link(temp, self.path)  # atomic publish; refuses an existing name
        except FileExistsError:
            found = inspect_lock(self.run_dir)
            raise RunLockError(found["outcome"] or RUN_LOCK_BUSY,
                               f"run {self.run_id} is held by {_describe(found['owner'])} "
                               f"(owner liveness: {found['liveness']})",
                               owner=found["owner"], liveness=found["liveness"]) from None
        finally:
            temp.unlink(missing_ok=True)
        self.record = record
        return record

    def assert_held(self) -> None:
        """Re-verify on disk that this controller still owns the run."""
        on_disk = read_lock(self.run_dir)
        if not self.record or not on_disk or on_disk.get("owner_token") != self.record["owner_token"]:
            raise RunLockError(RUN_LOCK_BUSY, f"run {self.run_id}: controller lock lost or replaced")

    def release(self) -> None:
        """Remove the lock only if it is still exactly ours."""
        if self.record is None:
            return
        on_disk = read_lock(self.run_dir)
        if on_disk and on_disk.get("owner_token") == self.record["owner_token"]:
            self.path.unlink(missing_ok=True)
        self.record = None

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _describe(record: Mapping[str, Any] | None) -> str:
    owner = (record or {}).get("owner") or {}
    return f"pid={owner.get('pid')} host={owner.get('host')} token={(record or {}).get('owner_token')}"


def reconcile_stale_lock(run_dir: Path, *, expected_owner_token: str, operator: str,
                         reason: str) -> dict[str, Any]:
    """Explicit operator action: retire a lock whose owner is *provably* dead.

    Refuses when the owner is alive or ambiguous, or when the lock on disk is
    not the exact one the operator inspected (`expected_owner_token`). The
    retired lock is preserved verbatim next to a reconciliation record.
    """
    if not operator or not operator.strip():
        raise RunLockError(RUN_LOCK_BUSY, "reconciliation requires a named operator")
    found = inspect_lock(run_dir)
    if not found["held"]:
        raise RunLockError(RUN_LOCK_BUSY, "no lock to reconcile")
    if (found["owner"] or {}).get("owner_token") != expected_owner_token:
        raise RunLockError(RUN_LOCK_BUSY, "lock on disk is not the one inspected; re-inspect first",
                           owner=found["owner"], liveness=found["liveness"])
    if found["outcome"] != RUN_LOCK_STALE:
        raise RunLockError(RUN_LOCK_BUSY, f"owner is not provably dead (liveness: {found['liveness']}); "
                           "refusing to take over an ambiguous or live lock",
                           owner=found["owner"], liveness=found["liveness"])
    retired = Path(run_dir) / f"controller.lock.retired.{expected_owner_token}.json"
    record = {"schema_version": SCHEMA_VERSION, "event": "RUN_LOCK_RECONCILED", "operator": operator,
              "reason": reason, "liveness": found["liveness"], "retired_lock": found["owner"],
              "reconciled_at": _now(), "reconciled_by": current_owner()}
    retired.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    lock_path(run_dir).unlink()
    return record
