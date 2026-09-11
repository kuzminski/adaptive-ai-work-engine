#!/usr/bin/env python3
"""AAW UX RUNTIME BRIDGE V0.1 — cooperative run cancellation.

This module exists so that a Stop button has something real to call. It owns
exactly two things:

  * one flag per run, settable from a thread that is not the runner thread;
  * the set of child processes that run is currently waiting on.

It contains no workflow logic, no routing logic and no ledger logic. It never
decides what cancelling *means* for the graph — the runner does that, by
raising `RunCancelled` at the next boundary it controls.

Ambient scoping mirrors `process_observation`: the token travels in a
`ContextVar` rather than through the adapter signature, so the documented
adapter boundary (`workflow_runner.execute_llm_node`) keeps its shape and
every existing substitute of it keeps working unchanged.

What cancellation can and cannot interrupt, stated honestly:

  * a child process AAW spawned — interrupted immediately. `terminate()` is
    `TerminateProcess` on Windows and `SIGTERM` on POSIX, escalated to
    `kill()` after a grace period. There is no cooperative shutdown handshake
    with provider CLIs because they offer none.
  * the runner's own frontier — interrupted at the next node boundary, and
    inside the wait for a child, because killing the child ends the wait.
  * provider-side work already dispatched — NOT interrupted. The remote turn
    may still complete and may still bill. Killing the local CLI process ends
    AAW's knowledge of it, not the work itself.
  * a partially written worktree — NOT rolled back. A killed IMPLEMENT node
    may leave edits behind. They stay on the branch for a human to inspect;
    the runner never reverts, exactly as it never merges.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


# How long a terminated child gets to exit before it is killed outright.
TERMINATE_GRACE_SECONDS = 5.0

# Why a run was cancelled. Closed set: the UX renders these directly.
CANCEL_REQUESTED_BY_USER = "REQUESTED_BY_USER"
CANCEL_REQUESTED_BY_SUPERVISOR = "REQUESTED_BY_SUPERVISOR"
CANCEL_SOURCES = (CANCEL_REQUESTED_BY_USER, CANCEL_REQUESTED_BY_SUPERVISOR)


class RunCancelled(RuntimeError):
    """Raised inside the runner when a cancellation request is honoured."""

    def __init__(self, reason: str, *, source: str = CANCEL_REQUESTED_BY_USER,
                 at_boundary: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.source = source
        self.at_boundary = at_boundary


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class CancellationToken:
    """One run's stop switch. Safe to call from any thread."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id
        self._lock = threading.RLock()
        self._requested = False
        self._reason: str | None = None
        self._source: str | None = None
        self._requested_at: str | None = None
        self._processes: set[subprocess.Popen[Any]] = set()
        self._terminated: list[dict[str, Any]] = []

    # ── state ────────────────────────────────────────────────────────────
    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id, "requested": self._requested, "reason": self._reason,
                "source": self._source, "requested_at": self._requested_at,
                "live_processes": len(self._processes),
                "terminated_processes": list(self._terminated),
            }

    # ── request ──────────────────────────────────────────────────────────
    def request(self, reason: str = "stop requested", *,
                source: str = CANCEL_REQUESTED_BY_USER) -> dict[str, Any]:
        """Ask the run to stop and terminate whatever it is waiting on.

        Idempotent: a second request records nothing new and re-terminates
        nothing. Returns what the request actually did, so the caller can
        report a real effect instead of an intention.
        """
        with self._lock:
            first = not self._requested
            if first:
                self._requested = True
                self._reason = str(reason)
                self._source = str(source)
                self._requested_at = _now()
            # Only the first request signals. A later spawn cannot escape:
            # `register` refuses once cancellation has landed and the caller
            # stops that child immediately, so there is nothing left for a
            # repeat request to do but report the same effect twice.
            live = list(self._processes) if first else []
        killed = [self._stop_process(process) for process in live]
        with self._lock:
            self._terminated.extend(killed)
            return {
                "accepted": True, "first_request": first, "reason": self._reason,
                "source": self._source, "requested_at": self._requested_at,
                "terminated_now": killed,
            }

    def raise_if_requested(self, boundary: str) -> None:
        """Fail-fast at a boundary the runner controls."""
        with self._lock:
            if not self._requested:
                return
            reason, source = self._reason or "stop requested", self._source or CANCEL_REQUESTED_BY_USER
        raise RunCancelled(reason, source=source, at_boundary=boundary)

    # ── child processes ──────────────────────────────────────────────────
    def register(self, process: subprocess.Popen[Any]) -> bool:
        """Track a live child. False means cancellation already won the race.

        The race matters: a request that lands between the pre-spawn check and
        the spawn itself would otherwise leave an untracked child running for
        the whole node. Registering returns False in that case and the caller
        stops the child immediately.
        """
        with self._lock:
            if self._requested:
                return False
            self._processes.add(process)
            return True

    def unregister(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.discard(process)

    def _stop_process(self, process: subprocess.Popen[Any]) -> dict[str, Any]:
        """Terminate one child, escalating to kill. Never raises."""
        record: dict[str, Any] = {
            "process_id": getattr(process, "pid", None), "at": _now(),
            "signal": None, "escalated_to_kill": False, "exit_code": None,
        }
        if process.poll() is not None:
            record["signal"] = "ALREADY_EXITED"
            record["exit_code"] = process.returncode
            return record
        try:
            process.terminate()
            record["signal"] = "TERMINATE"
        except Exception as exc:  # a child that vanished is already stopped
            record["signal"] = f"TERMINATE_FAILED: {exc}"
            return record
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                record["escalated_to_kill"] = True
            except Exception as exc:
                record["signal"] = f"KILL_FAILED: {exc}"
                return record
            try:
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                record["signal"] = "KILL_TIMED_OUT"
                return record
        except Exception:
            return record
        record["exit_code"] = process.returncode
        return record

    @contextmanager
    def track(self, process: subprocess.Popen[Any]) -> Iterator[bool]:
        """Register `process` for the duration of the wait on it."""
        tracked = self.register(process)
        if not tracked:
            self._stop_process(process)
        try:
            yield tracked
        finally:
            self.unregister(process)


_TOKEN: ContextVar[CancellationToken | None] = ContextVar("aaw_cancellation_token", default=None)


@contextmanager
def cancellation_scope(token: CancellationToken | None) -> Iterator[None]:
    """Make `token` the ambient token for everything called inside this block."""
    handle = _TOKEN.set(token)
    try:
        yield
    finally:
        _TOKEN.reset(handle)


def current_token() -> CancellationToken | None:
    return _TOKEN.get()


def check(boundary: str) -> None:
    """Fail-fast at a runner boundary when a token is in scope."""
    token = _TOKEN.get()
    if token is not None:
        token.raise_if_requested(boundary)
