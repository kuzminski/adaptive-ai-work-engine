#!/usr/bin/env python3
"""AAW UX RUNTIME BRIDGE V0.1 — loopback HTTP/SSE transport.

Transport only. Every route is a thin translation of one `aaw_bridge` call:
this module holds no workflow knowledge, no routing knowledge and no state of
its own beyond the bridge instance it serves.

Why stdlib HTTP + SSE, and nothing else
---------------------------------------
The project has no third-party dependency of any kind — no `package.json`, no
`requirements.txt`, and every import in every module resolves to the standard
library or to AAW itself. The UX target is an HTML canvas. Given those two
facts:

  * `http.server.ThreadingHTTPServer` is already available, and serving the
    canvas from the same origin removes the CORS and `file://` problems that
    would otherwise force a build step.
  * Server-Sent Events is a plain HTTP response, and the thing being streamed
    is an append-only journal with a monotonically increasing `sequence` —
    exactly the shape SSE's `id:`/`Last-Event-ID` resume was designed for.
  * WebSockets would need a framing library the stdlib does not provide.
    Long-polling would reimplement SSE's resume by hand. A desktop IPC channel
    would not reach a browser at all. tkinter would mean redrawing the canvas.

So: no new infrastructure, and resume comes free.

Security posture, stated plainly: bound to 127.0.0.1, single user, no
authentication, no origin allowlist beyond same-origin serving. It is a local
development boundary and must not be exposed to a network. Multi-user and
remote deployment are explicitly deferred.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import aaw_bridge
from aaw_bridge import AawBridge, BridgeError
from workflow_schema import WorkflowValidationError


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
UI_ROOT = Path(__file__).with_name("UI_PROTOTYPE")
DEFAULT_PAGE = "aaw-canvas-live.html"

# How often an idle SSE stream re-reads the journal, and how often it emits a
# keepalive comment so an intermediary does not close a quiet connection.
POLL_SECONDS = 0.25
KEEPALIVE_SECONDS = 15.0
MAX_BODY_BYTES = 4 * 1024 * 1024


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AAWBridge/0.1"
    protocol_version = "HTTP/1.1"
    bridge: AawBridge  # injected by `serve`

    # ── plumbing ─────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            sys.stderr.write("[bridge] %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: HTTPStatus, payload: Any, *, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _fail(self, status: HTTPStatus, code: str, message: str,
              diagnostics: Any = None) -> None:
        self._send(status, {"error": code, "message": message,
                            "diagnostics": diagnostics or []})

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _one(self, key: str, default: str | None = None) -> str | None:
        values = self._query().get(key)
        return values[0] if values else default

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise BridgeError("BODY_TOO_LARGE", f"request body exceeds {MAX_BODY_BYTES} bytes")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("MALFORMED_JSON", f"request body is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise BridgeError("MALFORMED_JSON", "request body must be a JSON object")
        return data

    # ── routing ──────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path).path
        try:
            if route in ("/", f"/{DEFAULT_PAGE}"):
                return self._serve_static(DEFAULT_PAGE)
            if route == "/api/contract":
                return self._send(HTTPStatus.OK, aaw_bridge.public_contract())
            if route == "/api/workflows":
                return self._send(HTTPStatus.OK, self.bridge.list_workflows())
            if route == "/api/workflow":
                return self._send(HTTPStatus.OK, self.bridge.load_workflow(self._require("workflow_id")))
            if route == "/api/workflow/graph":
                return self._send(HTTPStatus.OK, self.bridge.graph_projection(self._require("workflow_id")))
            if route == "/api/layout":
                return self._send(HTTPStatus.OK, self.bridge.load_layout(self._require("workflow_id")))
            if route == "/api/workflow/blank":
                return self._send(HTTPStatus.OK, {
                    "candidate": self.bridge.blank_workflow(self._require("workflow_id"))})
            if route == "/api/run/node":
                return self._send(HTTPStatus.OK, self.bridge.node_detail(
                    self._require("run_id"), self._require("node_id")))
            if route == "/api/run/worktree":
                return self._send(HTTPStatus.OK, self.bridge.run_worktree(self._require("run_id")))
            if route == "/api/run/resume/plan":
                return self._send(HTTPStatus.OK, self.bridge.plan_resume(
                    self._require("workflow_id"), self._require("source_run_id"),
                    self._require("from_node"),
                    worktree=(self.server.workspace[1] if self.server.workspace else None)))  # type: ignore[attr-defined]
            if route == "/api/runs":
                return self._send(HTTPStatus.OK, self.bridge.list_runs())
            if route == "/api/run":
                return self._send(HTTPStatus.OK, self.bridge.run_projection(
                    self._require("run_id"), include_events=self._one("events") == "1"))
            if route == "/api/run/events":
                return self._send(HTTPStatus.OK, self.bridge.events(
                    self._require("run_id"), since=int(self._one("since") or 0)))
            if route == "/api/run/stream":
                return self._stream(self._require("run_id"))
            if route.startswith("/ui/"):
                return self._serve_static(route[len("/ui/"):])
            return self._fail(HTTPStatus.NOT_FOUND, "NO_ROUTE", f"no GET route {route}")
        except BridgeError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, exc.code, str(exc), exc.diagnostics)
        except WorkflowValidationError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, "SCHEMA_INVALID", str(exc),
                              [exc.as_diagnostic()])
        except Exception as exc:  # a UI request must never take the server down
            return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "BRIDGE_FAILURE",
                              f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path).path
        try:
            body = self._body()
            if route == "/api/workflow/validate":
                return self._send(HTTPStatus.OK, self.bridge.validate_candidate(
                    body.get("candidate") or {}))
            if route == "/api/workflow/save":
                report = self.bridge.save_workflow(
                    str(body.get("workflow_id") or ""), body.get("candidate") or {},
                    base_semantic_hash=body.get("base_semantic_hash"))
                return self._send(HTTPStatus.OK, report)
            if route == "/api/layout/save":
                return self._send(HTTPStatus.OK, self.bridge.save_layout(
                    str(body.get("workflow_id") or ""), body.get("layout") or {}))
            if route == "/api/workflow/create":
                return self._send(HTTPStatus.OK, self.bridge.create_workflow(
                    str(body.get("workflow_id") or ""), body.get("candidate") or {}))
            if route == "/api/run/start":
                return self._send(HTTPStatus.OK, self._start_run(body))
            if route == "/api/run/reset-downstream":
                return self._send(HTTPStatus.OK, self.bridge.reset_downstream(
                    str(body.get("run_id") or ""), str(body.get("node_id") or "")))
            if route == "/api/run/cancel":
                return self._send(HTTPStatus.OK, self.bridge.cancel_run(
                    str(body.get("run_id") or ""),
                    reason=str(body.get("reason") or "stop requested from UX"),
                    join_timeout=5.0))
            if route == "/api/run/worktree/keep":
                return self._send(HTTPStatus.OK, self.bridge.keep_run_changes(
                    str(body.get("run_id") or "")))
            if route == "/api/run/worktree/discard":
                return self._send(HTTPStatus.OK, self.bridge.discard_run_changes(
                    str(body.get("run_id") or "")))
            if route == "/api/run/worktree/adopt":
                return self._send(HTTPStatus.OK, self.bridge.adopt_run_changes_as_baseline(
                    str(body.get("run_id") or "")))
            if route == "/api/run/human":
                return self._send(HTTPStatus.OK, self.bridge.resolve_human_decision(
                    str(body.get("run_id") or ""), str(body.get("verdict") or "")))
            return self._fail(HTTPStatus.NOT_FOUND, "NO_ROUTE", f"no POST route {route}")
        except BridgeError as exc:
            status = (HTTPStatus.CONFLICT if exc.code in (aaw_bridge.WRITE_STALE,
                                                          aaw_bridge.WRITE_ALREADY_EXISTS)
                      else HTTPStatus.BAD_REQUEST)
            return self._fail(status, exc.code, str(exc), exc.diagnostics)
        except WorkflowValidationError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, "SCHEMA_INVALID", str(exc),
                              [exc.as_diagnostic()])
        except Exception as exc:
            return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "BRIDGE_FAILURE",
                              f"{type(exc).__name__}: {exc}")

    def _require(self, key: str) -> str:
        value = self._one(key)
        if not value:
            raise BridgeError("MISSING_PARAMETER", f"{key} is required")
        return value

    def _start_run(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Start a run. The workspace comes from the server, never the client.

        A browser must not be able to name an arbitrary repository or worktree
        for the runner to operate on, so those are fixed when the server is
        launched. The client chooses the workflow and the goal.
        """
        workspace = self.server.workspace  # type: ignore[attr-defined]
        if workspace is None:
            raise BridgeError("NO_WORKSPACE",
                              "this server was started without --repo/--worktree; runs are disabled")
        repo, worktree = workspace
        return self.bridge.start_run(
            str(body.get("workflow_id") or ""),
            goal=str(body.get("goal") or ""), repo=repo, worktree=worktree,
            overrides=body.get("overrides") or None,
            preprocess_policy=str(body.get("preprocess_policy") or "OFF"),
            adapter=self.server.adapter,  # type: ignore[attr-defined]
            resume_from=body.get("resume_from") or None,
        )

    # ── SSE ──────────────────────────────────────────────────────────────
    def _stream(self, run_id: str) -> None:
        """Stream this run's journal, resumable from a known sequence.

        `Last-Event-ID` (sent automatically by the browser on reconnect) and
        the explicit `?since=` both mean the same thing: the highest sequence
        the consumer has already rendered. Everything after it is replayed once
        and then followed live. The consumer never replays its whole history.
        """
        try:
            since = int(self.headers.get("Last-Event-ID") or self._one("since") or 0)
        except ValueError:
            since = 0
        try:
            self.bridge.events(run_id, since=since)  # resolves the run, or refuses
        except BridgeError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, exc.code, str(exc))

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        last_beat = time.monotonic()
        settled_seen = False
        # Status is sent when it *changes* or when events moved the cursor.
        # Sending it every poll would put four frames a second on the wire
        # saying the same thing, and the client would redraw for each.
        reported: tuple[str | None, str | None, int] | None = None
        try:
            self._frame("hello", {"run_id": run_id, "since": since,
                                  "bridge_version": aaw_bridge.BRIDGE_VERSION})
            while True:
                batch = self.bridge.events(run_id, since=since)
                for event in batch["events"]:
                    since = int(event["sequence"])
                    self._frame("routing", event, event_id=since)
                    last_beat = time.monotonic()
                status = (batch["lifecycle"], batch["runner_status"], since)
                if status != reported:
                    self._frame("status", {
                        "run_id": run_id, "lifecycle": batch["lifecycle"],
                        "runner_status": batch["runner_status"], "last_sequence": since,
                    })
                    reported = status
                    last_beat = time.monotonic()
                if batch["lifecycle"] == aaw_bridge.RUN_SETTLED:
                    if settled_seen:
                        # One extra pass after settling, so the final events a
                        # run writes on its way out are never lost to a race
                        # between the last append and the thread exiting.
                        self._frame("done", {"run_id": run_id, "last_sequence": since,
                                             "runner_status": batch["runner_status"]})
                        return
                    settled_seen = True
                else:
                    settled_seen = False
                if time.monotonic() - last_beat > KEEPALIVE_SECONDS:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                time.sleep(POLL_SECONDS)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return  # the browser navigated away; nothing to clean up
        except BridgeError:
            return

    def _frame(self, event: str, payload: Any, *, event_id: int | None = None) -> None:
        chunk = ""
        if event_id is not None:
            chunk += f"id: {event_id}\n"
        chunk += f"event: {event}\n"
        chunk += "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    # ── static ───────────────────────────────────────────────────────────
    def _serve_static(self, relative: str) -> None:
        """Serve the canvas from the same origin. Confined to UI_PROTOTYPE."""
        root = UI_ROOT.resolve()
        try:
            target = (root / relative).resolve()
            target.relative_to(root)
        except (ValueError, OSError):
            return self._fail(HTTPStatus.FORBIDDEN, "OUTSIDE_UI_ROOT", "path escapes the UI root")
        if not target.is_file():
            return self._fail(HTTPStatus.NOT_FOUND, "NO_FILE", f"no UI file {relative}")
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind == "application/javascript":
            kind += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type=kind)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, bridge: AawBridge,
                 workspace: tuple[Path, Path] | None = None, adapter: Any = None,
                 verbose: bool = False) -> None:
        handler = type("BoundBridgeHandler", (BridgeHandler,), {"bridge": bridge})
        super().__init__(address, handler)
        self.bridge = bridge
        self.workspace = workspace
        self.adapter = adapter
        self.verbose = verbose


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, bridge: AawBridge | None = None,
          workspace: tuple[Path, Path] | None = None, adapter: Any = None,
          verbose: bool = False) -> BridgeServer:
    """Create and start a bridge server on a background thread."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the bridge binds loopback only; remote deployment is out of scope")
    server = BridgeServer((host, port), bridge=bridge or aaw_bridge.default_bridge(),
                          workspace=workspace, adapter=adapter, verbose=verbose)
    threading.Thread(target=server.serve_forever, name="aaw-bridge-http", daemon=True).start()
    return server


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="AAW UX runtime bridge (loopback HTTP/SSE)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--repo", type=Path, help="canonical repository a run may read")
    ap.add_argument("--worktree", type=Path, help="isolated worktree a run may write")
    ap.add_argument("--scripted-adapter", type=Path,
                    help="JSON script replacing paid provider calls; routing/runtime stay real")
    ap.add_argument("--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    adapter = None
    if args.scripted_adapter:
        import aaw_llm_test_adapter
        adapter = aaw_llm_test_adapter.scripted_adapter(
            aaw_llm_test_adapter.load_script(args.scripted_adapter))
    workspace = (args.repo, args.worktree) if args.repo and args.worktree else None
    server = serve(host=args.host, port=args.port, workspace=workspace,
                   adapter=adapter, verbose=args.verbose)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"AAW UX runtime bridge on {url}")
    print(f"  contract   {url}api/contract")
    print(f"  workflows  {url}api/workflows")
    if workspace is None:
        print("  runs DISABLED: pass --repo and --worktree to enable starting runs")
    else:
        print(f"  workspace  repo={args.repo}  worktree={args.worktree}")
    if adapter is not None:
        print(f"  adapter    SCRIPTED ({args.scripted_adapter}) - no paid provider calls")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
