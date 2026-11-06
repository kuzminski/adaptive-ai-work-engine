#!/usr/bin/env python3
"""AAW UNIVERSAL MCP SERVER — Model Context Protocol stdlib transport.

Pure standard library implementation of MCP (Model Context Protocol,
JSON-RPC 2.0) for the Adaptive AI Work Engine.

Provides tool access, resource inspection, and status monitoring for external
orchestrators and IDEs while enforcing AAW's core invariants:
  - No automated Human Gate approval (requires direct operator action).
  - No automated git merge or push to main/remote.
  - Zero external pip dependencies.

Transports:
  - serve_stdio: line-delimited JSON-RPC over stdin/stdout.
  - serve_http: loopback HTTP (127.0.0.1) POST /mcp endpoint.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import secrets
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import product_home
import product_providers
import product_recommendations
import product_runs
import product_version
import product_view

SERVER_NAME = "aaw-mcp"
SERVER_VERSION = product_version.RELEASE
PROTOCOL_VERSION = "2024-11-05"

HUMAN_GATE_INVARIANT_MESSAGE = (
    "Human Gate approval and git merge/push cannot be automated via MCP. "
    "Direct operator action is required."
)

FORBIDDEN_GATE_KEYWORDS = (
    "approve",
    "approval",
    "accept",
    "promote",
    "gate_decision",
    "human_gate",
    "merge",
    "push",
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "aaw_detect_providers",
        "description": "Detect locally installed AI CLI providers (Claude, Codex, Antigravity) and model availability.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "aaw_inspect_repo",
        "description": "Inspect a git repository path for AAW task suitability, branches, commit head and status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute filesystem path to the git repository to inspect.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aaw_preview_task",
        "description": "Validate and preview an AAW task form without starting it, returning resolved model bindings and readiness blockers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "form": {
                    "type": "object",
                    "description": "Task creation form containing goal, repo, choices, and roadmap.",
                },
            },
            "required": ["form"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aaw_start_task",
        "description": "Start a new autonomous run with the specified task form in an isolated worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "form": {
                    "type": "object",
                    "description": "Task creation form containing goal, repo, choices, and roadmap.",
                },
            },
            "required": ["form"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aaw_run_status",
        "description": "Get current status, timeline, process stages, and human gate state of an autonomous run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Identifier of the run (e.g. AAW_TASK_...).",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aaw_run_evidence",
        "description": "Get raw evidence artifacts, journal events, or execution descriptor for a run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Identifier of the run.",
                },
                "execution_id": {
                    "type": "string",
                    "description": "Optional specific execution ID to inspect.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aaw_run_stop",
        "description": "Request a safe stop or force stop for a running autonomous task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Identifier of the run.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Whether to force-stop immediately rather than waiting for next phase boundary.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aaw_run_resume",
        "description": "Resume a paused or interrupted autonomous task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Identifier of the run.",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
]

RESOURCES: list[dict[str, Any]] = [
    {
        "uri": "aaw://catalog/models",
        "name": "Model recommendations catalog",
        "mimeType": "application/json",
        "description": "Effective model recommendations and choices catalog.",
    },
    {
        "uri": "aaw://runs/{run_id}/state",
        "name": "Run state and iterations",
        "mimeType": "application/json",
        "description": "Task definition, execution state, and iterations for a run.",
    },
]


def is_forbidden_gate_or_push(tool_name: str, arguments: Mapping[str, Any] | None) -> bool:
    """Detect whether a tool invocation attempts to automate Human Gate approval or git merge/push."""
    name_lower = (tool_name or "").lower()
    for kw in FORBIDDEN_GATE_KEYWORDS:
        if kw in name_lower:
            return True
    if isinstance(arguments, dict):
        action = str(arguments.get("action") or "").lower()
        if any(kw in action for kw in ("approve", "accept", "promote", "merge", "push")):
            return True
        decision = str(arguments.get("decision") or "").lower()
        if any(kw in decision for kw in ("approve", "accept", "promote")):
            return True
        cmd = str(arguments.get("command") or "").lower()
        if any(kw in cmd for kw in ("git push", "git merge", "push", "merge")):
            return True
        if (
            arguments.get("auto_approve")
            or arguments.get("approve")
            or arguments.get("git_push")
            or arguments.get("merge")
            or arguments.get("push")
        ):
            return True
    return False


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


class AawMcpServer:
    """In-memory dispatcher for MCP JSON-RPC 2.0 protocol methods."""

    def __init__(self) -> None:
        self.name = SERVER_NAME
        self.version = SERVER_VERSION

    def handle_message(self, message: str | bytes | Mapping[str, Any]) -> dict[str, Any] | None:
        """Process one JSON-RPC 2.0 message and return response dict, or None for notifications."""
        if isinstance(message, (str, bytes)):
            try:
                data = json.loads(message)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
        elif isinstance(message, Mapping):
            data = dict(message)
        else:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: message must be JSON object"},
            }

        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or "method" not in data:
            req_id = data.get("id") if isinstance(data, dict) else None
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: expected JSON-RPC 2.0 object with method"},
            }

        method = str(data["method"])
        params = data.get("params")
        is_notification = "id" not in data or data.get("id") is None

        # Notifications never generate responses in JSON-RPC 2.0
        if is_notification:
            try:
                self.dispatch(method, params)
            except Exception:
                pass
            return None

        req_id = data.get("id")
        try:
            result = self.dispatch(method, params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except JsonRpcError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": exc.to_dict()}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": f"Internal error: {exc}"}}

    handle_request = handle_message

    def dispatch(self, method: str, params: Any) -> Any:
        """Route method to implementation handler."""
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                    "resources": {},
                },
                "serverInfo": {
                    "name": self.name,
                    "version": self.version,
                },
            }
        elif method == "notifications/initialized":
            return None
        elif method == "ping":
            return {}
        elif method == "tools/list":
            return {"tools": TOOLS}
        elif method == "tools/call":
            if not isinstance(params, dict):
                return {
                    "content": [{"type": "text", "text": "Invalid params: 'tools/call' requires an object"}],
                    "isError": True,
                }
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return {
                    "content": [{"type": "text", "text": "Invalid params: 'arguments' must be an object"}],
                    "isError": True,
                }
            return self.call_tool(name, arguments)
        elif method == "resources/list":
            return {"resources": RESOURCES}
        elif method == "resources/read":
            if not isinstance(params, dict) or "uri" not in params:
                raise JsonRpcError(-32602, "Invalid params: 'resources/read' requires 'uri'")
            return self.read_resource(str(params["uri"]))
        else:
            raise JsonRpcError(-32601, f"Method not found: '{method}'")

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one tool with safety invariants and parameter checks."""
        # Critical safety check
        if is_forbidden_gate_or_push(name, arguments):
            return {
                "content": [{"type": "text", "text": HUMAN_GATE_INVARIANT_MESSAGE}],
                "isError": True,
            }

        valid_names = {t["name"] for t in TOOLS}
        if name not in valid_names:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: '{name}'"}],
                "isError": True,
            }

        try:
            if name == "aaw_detect_providers":
                output = product_providers.detect_all()
            elif name == "aaw_inspect_repo":
                path = arguments.get("path")
                if not isinstance(path, str) or not path.strip():
                    return {
                        "content": [{"type": "text", "text": "Missing required argument 'path'"}],
                        "isError": True,
                    }
                output = product_runs.inspect_repo(path)
            elif name == "aaw_preview_task":
                form = arguments.get("form")
                if not isinstance(form, dict):
                    return {
                        "content": [{"type": "text", "text": "Missing required argument 'form' (must be an object)"}],
                        "isError": True,
                    }
                raw_preview = product_runs.preview_task(form)
                output = product_runs.public_preview(raw_preview)
            elif name == "aaw_start_task":
                form = arguments.get("form")
                if not isinstance(form, dict):
                    return {
                        "content": [{"type": "text", "text": "Missing required argument 'form' (must be an object)"}],
                        "isError": True,
                    }
                output = product_runs.start_task(form)
            elif name == "aaw_run_status":
                run_id = arguments.get("run_id")
                if not isinstance(run_id, str) or not run_id.strip():
                    return {
                        "content": [{"type": "text", "text": "Missing required argument 'run_id'"}],
                        "isError": True,
                    }
                if not re.fullmatch(r"[A-Za-z0-9_\-]{3,80}", run_id.strip()):
                    return {
                        "content": [{"type": "text", "text": f"Invalid run_id format: '{run_id}'"}],
                        "isError": True,
                    }
                output = product_view.run_view(run_id.strip())
            elif name == "aaw_run_evidence":
                run_id = arguments.get("run_id")
                if not isinstance(run_id, str) or not run_id.strip():
                    return {
                        "content": [{"type": "text", "text": "Missing required argument 'run_id'"}],
                        "isError": True,
                    }
                if not re.fullmatch(r"[A-Za-z0-9_\-]{3,80}", run_id.strip()):
                    return {
                        "content": [{"type": "text", "text": f"Invalid run_id format: '{run_id}'"}],
                        "isError": True,
                    }
                execution_id = arguments.get("execution_id")
                output = product_view.evidence_view(run_id.strip(), execution_id=execution_id)
            elif name == "aaw_run_stop":
                run_id = arguments.get("run_id")
                if not isinstance(run_id, str) or not run_id.strip():
                    return {
                        "content": [{"type": "text", "text": "Missing required argument 'run_id'"}],
                        "isError": True,
                    }
                if not re.fullmatch(r"[A-Za-z0-9_\-]{3,80}", run_id.strip()):
                    return {
                        "content": [{"type": "text", "text": f"Invalid run_id format: '{run_id}'"}],
                        "isError": True,
                    }
                force = bool(arguments.get("force", False))
                output = product_runs.request_stop(run_id.strip(), force=force)
            elif name == "aaw_run_resume":
                run_id = arguments.get("run_id")
                if not isinstance(run_id, str) or not run_id.strip():
                    return {
                        "content": [{"type": "text", "text": "Missing required argument 'run_id'"}],
                        "isError": True,
                    }
                if not re.fullmatch(r"[A-Za-z0-9_\-]{3,80}", run_id.strip()):
                    return {
                        "content": [{"type": "text", "text": f"Invalid run_id format: '{run_id}'"}],
                        "isError": True,
                    }
                output = product_runs.resume_task(run_id.strip())
            else:
                return {
                    "content": [{"type": "text", "text": f"Unhandled tool '{name}'"}],
                    "isError": True,
                }

            text = json.dumps(output, ensure_ascii=False, indent=2, default=str)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }

    def read_resource(self, uri: str) -> dict[str, Any]:
        """Read content for a registered resource URI."""
        if uri == "aaw://catalog/models":
            catalog = product_recommendations.effective_catalog()
            text = json.dumps(catalog, ensure_ascii=False, indent=2, default=str)
            return {
                "contents": [
                    {
                        "uri": "aaw://catalog/models",
                        "mimeType": "application/json",
                        "text": text,
                    }
                ]
            }

        run_match = re.match(r"^aaw://runs/([^/]+)/state$", uri)
        if run_match:
            raw_id = run_match.group(1)
            run_id = raw_id
            if raw_id == "{run_id}":
                run_ids = product_runs.list_run_ids()
                if run_ids:
                    run_id = run_ids[0]
                else:
                    task = {"schema_version": product_runs.TASK_SCHEMA, "run_id": "{run_id}"}
                    state = {"status": "INITIAL", "iterations": []}
                    payload = {
                        "run_id": "{run_id}",
                        "task": task,
                        "state": state,
                        "iterations": [],
                    }
                    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
                    return {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "application/json",
                                "text": text,
                            }
                        ]
                    }
            elif not re.fullmatch(r"[A-Za-z0-9_\-]{3,80}", run_id):
                raise JsonRpcError(-32602, f"Invalid run_id format: {run_id!r}")

            try:
                task = product_runs.load_task(run_id)
            except Exception:
                task = None
            try:
                state_path = product_runs.autonomy_dir(run_id) / "autonomy_state.json"
                state = product_home.read_json(state_path)
            except Exception:
                state = None
            if task is None and state is None:
                if raw_id == "{run_id}":
                    # Fallback template preview if no runs exist yet
                    task = {"schema_version": product_runs.TASK_SCHEMA, "run_id": "{run_id}"}
                    state = {"status": "INITIAL", "iterations": []}
                else:
                    raise JsonRpcError(-32602, f"Run {run_id!r} not found")

            payload = {
                "run_id": run_id,
                "task": task,
                "state": state,
                "iterations": (state or {}).get("iterations", []),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": text,
                    }
                ]
            }

        raise JsonRpcError(-32602, f"Resource not found: '{uri}'")


# ── stdio transport ──────────────────────────────────────────────────────────


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Serve JSON-RPC line-delimited protocol over stdin / stdout."""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    server = AawMcpServer()

    for line in in_stream:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            response = server.handle_message(stripped)
        except Exception as exc:
            sys.stderr.write(f"[aaw-mcp] error processing message: {exc}\n")
            sys.stderr.flush()
            continue

        if response is not None:
            out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            out_stream.flush()

    return 0


# ── loopback HTTP transport ──────────────────────────────────────────────────

MAX_HTTP_PAYLOAD_BYTES = 2 * 1024 * 1024  # 2 MiB request body limit


def resolve_mcp_auth_token(token: str | None = None) -> str:
    """Resolve or generate the secure token required for MCP HTTP transport."""
    if token and token.strip():
        return token.strip()
    token_file = product_home.home() / "mcp_token.txt"
    if token_file.is_file():
        saved = token_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    # Check ui.json as fallback
    ui_json = product_home.home() / "ui.json"
    if ui_json.is_file():
        ui_data = product_home.read_json(ui_json)
        if isinstance(ui_data, dict) and ui_data.get("token"):
            return str(ui_data["token"]).strip()
    # Generate fresh token and persist
    new_token = secrets.token_urlsafe(24)
    try:
        token_file.write_text(new_token, encoding="utf-8")
    except Exception:
        pass
    return new_token


class McpHttpHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    server: McpHttpServer

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress standard access logs to keep terminal quiet."""
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip()
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (parsed.hostname or "") in ("127.0.0.1", "localhost", "::1")

    def _authorized(self) -> bool:
        if not self.server.auth_token:
            return True
        token = self.headers.get("X-AAW-Token") or ""
        if not token:
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
        return bool(token) and secrets.compare_digest(token, self.server.auth_token)

    def _send_json_error(self, status: int, message: str) -> None:
        payload = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send_json_error(HTTPStatus.FORBIDDEN, "Forbidden: Host must be loopback")
            return
        if not self._origin_ok():
            self._send_json_error(HTTPStatus.FORBIDDEN, "Forbidden: Cross-origin access rejected")
            return
        if not self._authorized():
            self._send_json_error(HTTPStatus.UNAUTHORIZED, "Unauthorized: valid X-AAW-Token or Authorization Bearer token required")
            return

        if self.path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        content_length_str = self.headers.get("Content-Length")
        try:
            content_length = int(content_length_str or 0)
        except ValueError:
            self._send_json_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return

        if content_length > MAX_HTTP_PAYLOAD_BYTES:
            self._send_json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Payload Too Large (max 2 MiB)")
            return

        try:
            body = self.rfile.read(content_length).decode("utf-8")
        except Exception as exc:
            self._send_json_error(HTTPStatus.BAD_REQUEST, f"Failed to read request body: {exc}")
            return

        try:
            response = self.server.mcp_server.handle_message(body)
        except Exception as exc:
            self._send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal JSON-RPC dispatch error: {exc}")
            return

        if response is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send_json_error(HTTPStatus.FORBIDDEN, "Forbidden: Host must be loopback")
            return
        if not self._origin_ok():
            self._send_json_error(HTTPStatus.FORBIDDEN, "Forbidden: Cross-origin access rejected")
            return

        if self.path in ("/ping", "/health"):
            payload = json.dumps(
                {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION}
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        elif self.path == "/mcp":
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Method Not Allowed: use POST for JSON-RPC")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")


class McpHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        mcp_server: AawMcpServer | None = None,
        auth_token: str | None = None,
    ) -> None:
        host = server_address[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"Universal MCP HTTP server must bind to loopback (127.0.0.1), got {host!r}")
        super().__init__(server_address, RequestHandlerClass)
        self.mcp_server = mcp_server or AawMcpServer()
        self.auth_token = resolve_mcp_auth_token(auth_token)


def create_http_server(
    host: str = "127.0.0.1", port: int = 8788, mcp_server: AawMcpServer | None = None,
    auth_token: str | None = None,
) -> McpHttpServer:
    """Instantiate a loopback ThreadingHTTPServer with /mcp endpoint and token auth."""
    return McpHttpServer((host, port), McpHttpHandler, mcp_server=mcp_server, auth_token=auth_token)


def serve_http(
    host: str = "127.0.0.1", port: int = 8788, *, auth_token: str | None = None,
    server: McpHttpServer | None = None
) -> int:
    """Run loopback ThreadingHTTPServer serving POST /mcp endpoint."""
    httpd = server or create_http_server(host, port, auth_token=auth_token)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    return 0


# ── CLI entrypoint ───────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="AAW Universal MCP Server")
    parser.add_argument("--http", action="store_true", help="Serve via loopback HTTP")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8788, help="HTTP port (default: 8788)")
    parser.add_argument("--token", default=None, help="Auth token for HTTP mode")
    args = parser.parse_args()

    if args.http:
        return serve_http(host=args.host, port=args.port, auth_token=args.token)
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
