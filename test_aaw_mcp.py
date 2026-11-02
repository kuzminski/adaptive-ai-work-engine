"""AAW Universal MCP Server Automated Test Suite.

Validates:
- Pure stdlib JSON-RPC 2.0 lifecycle (initialize, ping, notifications/initialized).
- MCP tools/list and tools/call (detect_providers, inspect_repo, preview_task, run_status, etc.).
- MCP resources/list and resources/read (catalog, run state).
- CRITICAL INVARIANT: Rejects any attempt to automate Human Gate approval or git merge/push.
- Stdio transport with io.StringIO simulation.
- Loopback HTTP transport (/mcp endpoint, notifications, status codes).
"""

from __future__ import annotations

import io
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aaw_mcp_server as mcp
import product_home
import product_runs
import product_view


@pytest.fixture
def server() -> mcp.AawMcpServer:
    return mcp.AawMcpServer()


# ── 1. Protocol Handshake ───────────────────────────────────────────────────


def test_protocol_handshake_initialize(server: mcp.AawMcpServer) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": "init-1",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }
    resp = server.handle_message(req)
    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "init-1"

    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"] == {"name": "aaw-mcp", "version": "0.4.0"}
    assert result["capabilities"] == {"tools": {}, "resources": {}}


def test_protocol_handshake_ping(server: mcp.AawMcpServer) -> None:
    req = {"jsonrpc": "2.0", "id": 100, "method": "ping"}
    resp = server.handle_message(req)
    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 100
    assert resp["result"] == {}


def test_protocol_handshake_notifications_initialized(server: mcp.AawMcpServer) -> None:
    # Notification has no "id" member: must return None (no response emitted)
    notif = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    resp = server.handle_message(notif)
    assert resp is None

    # Direct dispatch test
    assert server.dispatch("notifications/initialized", {}) is None


def test_protocol_errors(server: mcp.AawMcpServer) -> None:
    # Invalid JSON string
    resp = server.handle_message("{invalid json")
    assert resp is not None
    assert resp["error"]["code"] == -32700

    # Non-JSON-RPC dict
    resp = server.handle_message({"not": "jsonrpc"})
    assert resp is not None
    assert resp["error"]["code"] == -32600

    # Method not found
    resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "non_existent_method"})
    assert resp is not None
    assert resp["error"]["code"] == -32601


# ── 2. Tools List ────────────────────────────────────────────────────────────


def test_tools_list(server: mcp.AawMcpServer) -> None:
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    resp = server.handle_message(req)
    assert resp is not None
    assert "result" in resp
    tools = resp["result"]["tools"]

    expected_tools = {
        "aaw_detect_providers",
        "aaw_inspect_repo",
        "aaw_preview_task",
        "aaw_start_task",
        "aaw_run_status",
        "aaw_run_evidence",
        "aaw_run_stop",
        "aaw_run_resume",
    }
    actual_tools = {t["name"] for t in tools}
    assert expected_tools.issubset(actual_tools)

    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema

    # Specific schema checks
    by_name = {t["name"]: t for t in tools}
    assert by_name["aaw_inspect_repo"]["inputSchema"]["required"] == ["path"]
    assert by_name["aaw_preview_task"]["inputSchema"]["required"] == ["form"]
    assert by_name["aaw_start_task"]["inputSchema"]["required"] == ["form"]
    assert by_name["aaw_run_status"]["inputSchema"]["required"] == ["run_id"]
    assert by_name["aaw_run_evidence"]["inputSchema"]["required"] == ["run_id"]
    assert by_name["aaw_run_stop"]["inputSchema"]["required"] == ["run_id"]
    assert by_name["aaw_run_resume"]["inputSchema"]["required"] == ["run_id"]


# ── 3. Tools Call ────────────────────────────────────────────────────────────


def test_tools_call_detect_providers(server: mcp.AawMcpServer) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": "call-1",
        "method": "tools/call",
        "params": {"name": "aaw_detect_providers", "arguments": {}},
    }
    resp = server.handle_message(req)
    assert resp is not None
    assert resp["id"] == "call-1"
    result = resp["result"]
    assert result["isError"] is False
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"

    data = json.loads(result["content"][0]["text"])
    assert "providers" in data
    assert "git" in data


def test_tools_call_inspect_repo(server: mcp.AawMcpServer) -> None:
    # Success path
    req = {
        "jsonrpc": "2.0",
        "id": "call-2",
        "method": "tools/call",
        "params": {
            "name": "aaw_inspect_repo",
            "arguments": {"path": str(ROOT)},
        },
    }
    resp = server.handle_message(req)
    assert resp is not None
    result = resp["result"]
    assert result["isError"] is False
    data = json.loads(result["content"][0]["text"])
    assert "ready" in data
    assert "head" in data

    # Error path: missing argument
    err_req = {
        "jsonrpc": "2.0",
        "id": "call-2-err",
        "method": "tools/call",
        "params": {"name": "aaw_inspect_repo", "arguments": {}},
    }
    err_resp = server.handle_message(err_req)
    assert err_resp is not None
    assert err_resp["result"]["isError"] is True
    assert "path" in err_resp["result"]["content"][0]["text"].lower()


def test_tools_call_preview_task(server: mcp.AawMcpServer) -> None:
    # Success path
    form = {
        "repo": str(ROOT),
        "goal": "Verify MCP server integration",
        "choices": {"planning": "BALANCED", "implementation": "BALANCED", "review": "BALANCED"},
    }
    req = {
        "jsonrpc": "2.0",
        "id": "call-3",
        "method": "tools/call",
        "params": {"name": "aaw_preview_task", "arguments": {"form": form}},
    }
    resp = server.handle_message(req)
    assert resp is not None
    result = resp["result"]
    assert result["isError"] is False
    data = json.loads(result["content"][0]["text"])
    assert "can_start" in data
    assert "goal" in data

    # Error path: missing form
    err_req = {
        "jsonrpc": "2.0",
        "id": "call-3-err",
        "method": "tools/call",
        "params": {"name": "aaw_preview_task", "arguments": {}},
    }
    err_resp = server.handle_message(err_req)
    assert err_resp is not None
    assert err_resp["result"]["isError"] is True
    assert "form" in err_resp["result"]["content"][0]["text"].lower()


def test_tools_call_run_status(server: mcp.AawMcpServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # Error path: missing run_id
    err_req = {
        "jsonrpc": "2.0",
        "id": "call-4-err",
        "method": "tools/call",
        "params": {"name": "aaw_run_status", "arguments": {}},
    }
    err_resp = server.handle_message(err_req)
    assert err_resp is not None
    assert err_resp["result"]["isError"] is True

    # Error path: non-existent run
    missing_req = {
        "jsonrpc": "2.0",
        "id": "call-4-missing",
        "method": "tools/call",
        "params": {"name": "aaw_run_status", "arguments": {"run_id": "AAW_TASK_NON_EXISTENT_999"}},
    }
    missing_resp = server.handle_message(missing_req)
    assert missing_resp is not None
    assert missing_resp["result"]["isError"] is True

    # Success path with mock
    monkeypatch.setattr(
        product_view, "run_view", lambda run_id: {"run_id": run_id, "status": "RUNNING", "section": "running"}
    )
    ok_req = {
        "jsonrpc": "2.0",
        "id": "call-4-ok",
        "method": "tools/call",
        "params": {"name": "aaw_run_status", "arguments": {"run_id": "AAW_TASK_TEST_001"}},
    }
    ok_resp = server.handle_message(ok_req)
    assert ok_resp is not None
    assert ok_resp["result"]["isError"] is False
    data = json.loads(ok_resp["result"]["content"][0]["text"])
    assert data["run_id"] == "AAW_TASK_TEST_001"
    assert data["status"] == "RUNNING"


def test_tools_call_additional_tools(server: mcp.AawMcpServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # aaw_run_evidence
    monkeypatch.setattr(
        product_view, "evidence_view", lambda run_id, execution_id=None: {"run_id": run_id, "executions": []}
    )
    ev_req = {
        "jsonrpc": "2.0",
        "id": "ev-1",
        "method": "tools/call",
        "params": {"name": "aaw_run_evidence", "arguments": {"run_id": "RUN_X"}},
    }
    ev_resp = server.handle_message(ev_req)
    assert ev_resp["result"]["isError"] is False

    # aaw_run_stop
    monkeypatch.setattr(product_runs, "request_stop", lambda run_id, force=False: {"stopped": True, "force": force})
    stop_req = {
        "jsonrpc": "2.0",
        "id": "stop-1",
        "method": "tools/call",
        "params": {"name": "aaw_run_stop", "arguments": {"run_id": "RUN_X", "force": True}},
    }
    stop_resp = server.handle_message(stop_req)
    assert stop_resp["result"]["isError"] is False
    assert json.loads(stop_resp["result"]["content"][0]["text"])["force"] is True

    # aaw_run_resume
    monkeypatch.setattr(product_runs, "resume_task", lambda run_id: {"resumed": True, "run_id": run_id})
    res_req = {
        "jsonrpc": "2.0",
        "id": "res-1",
        "method": "tools/call",
        "params": {"name": "aaw_run_resume", "arguments": {"run_id": "RUN_X"}},
    }
    res_resp = server.handle_message(res_req)
    assert res_resp["result"]["isError"] is False

    # aaw_start_task
    monkeypatch.setattr(product_runs, "start_task", lambda form: {"run_id": "AAW_TASK_NEW", "pid": 1234})
    start_req = {
        "jsonrpc": "2.0",
        "id": "start-1",
        "method": "tools/call",
        "params": {"name": "aaw_start_task", "arguments": {"form": {"goal": "New task"}}},
    }
    start_resp = server.handle_message(start_req)
    assert start_resp["result"]["isError"] is False


# ── 4. Critical Invariant (Human Gate & Git Push/Merge) ──────────────────────


@pytest.mark.parametrize(
    "tool_name",
    [
        "aaw_approve",
        "aaw_accept",
        "aaw_promote",
        "aaw_human_gate",
        "aaw_gate_decision",
        "aaw_git_merge",
        "aaw_git_push",
        "approve_task",
        "git_push",
        "accept_gate",
    ],
)
def test_invariant_rejects_forbidden_tool_names(server: mcp.AawMcpServer, tool_name: str) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": f"inv-{tool_name}",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {"run_id": "RUN_123"}},
    }
    resp = server.handle_message(req)
    assert resp is not None
    result = resp["result"]
    assert result["isError"] is True
    err_text = result["content"][0]["text"]
    assert "Human Gate approval and git merge/push cannot be automated via MCP" in err_text
    assert "Direct operator action is required" in err_text


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "approve"},
        {"action": "accept"},
        {"action": "promote"},
        {"command": "git push origin main"},
        {"command": "git merge aaw/task"},
        {"auto_approve": True},
        {"approve": True},
        {"git_push": True},
        {"merge": True},
        {"push": True},
        {"decision": "accept"},
    ],
)
def test_invariant_rejects_forbidden_arguments(server: mcp.AawMcpServer, arguments: dict[str, Any]) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": "inv-arg",
        "method": "tools/call",
        "params": {"name": "aaw_start_task", "arguments": arguments},
    }
    resp = server.handle_message(req)
    assert resp is not None
    result = resp["result"]
    assert result["isError"] is True
    err_text = result["content"][0]["text"]
    assert "Human Gate approval and git merge/push cannot be automated via MCP" in err_text
    assert "Direct operator action is required" in err_text


# ── 5. Resources List and Read ───────────────────────────────────────────────


def test_resources_list(server: mcp.AawMcpServer) -> None:
    req = {"jsonrpc": "2.0", "id": "res-list", "method": "resources/list"}
    resp = server.handle_message(req)
    assert resp is not None
    resources = resp["result"]["resources"]

    uris = {r["uri"] for r in resources}
    assert "aaw://catalog/models" in uris
    assert "aaw://runs/{run_id}/state" in uris

    by_uri = {r["uri"]: r for r in resources}
    assert by_uri["aaw://catalog/models"]["name"] == "Model recommendations catalog"
    assert by_uri["aaw://runs/{run_id}/state"]["name"] == "Run state and iterations"


def test_resources_read_catalog(server: mcp.AawMcpServer) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": "res-read-cat",
        "method": "resources/read",
        "params": {"uri": "aaw://catalog/models"},
    }
    resp = server.handle_message(req)
    assert resp is not None
    contents = resp["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "aaw://catalog/models"
    assert contents[0]["mimeType"] == "application/json"

    catalog = json.loads(contents[0]["text"])
    assert "catalog_version" in catalog
    assert "choices" in catalog
    assert "profiles" in catalog


def test_resources_read_run_state(server: mcp.AawMcpServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Template direct read
    req_tpl = {
        "jsonrpc": "2.0",
        "id": "res-read-tpl",
        "method": "resources/read",
        "params": {"uri": "aaw://runs/{run_id}/state"},
    }
    resp_tpl = server.handle_message(req_tpl)
    assert resp_tpl is not None
    contents_tpl = resp_tpl["result"]["contents"]
    assert len(contents_tpl) == 1
    data_tpl = json.loads(contents_tpl[0]["text"])
    assert "task" in data_tpl
    assert "state" in data_tpl

    # 1b. Template direct read when no runs exist on the machine (reproduces CI clean runner)
    monkeypatch.setattr(product_runs, "list_run_ids", lambda: [])
    resp_empty = server.handle_message(req_tpl)
    assert resp_empty is not None
    assert "result" in resp_empty
    contents_empty = resp_empty["result"]["contents"]
    assert len(contents_empty) == 1
    data_empty = json.loads(contents_empty[0]["text"])
    assert data_empty["run_id"] == "{run_id}"
    assert data_empty["state"]["status"] == "INITIAL"

    # 2. Specific mocked run
    test_run_id = "AAW_TASK_MOCK_123"
    monkeypatch.setattr(
        product_runs, "load_task", lambda run_id: {"run_id": run_id, "form": {"goal": "Mock goal"}}
    )
    monkeypatch.setattr(
        product_home, "read_json", lambda path, default=None: {"status": "RUNNING", "iterations": [{"index": 1}]}
    )

    req_run = {
        "jsonrpc": "2.0",
        "id": "res-read-run",
        "method": "resources/read",
        "params": {"uri": f"aaw://runs/{test_run_id}/state"},
    }
    resp_run = server.handle_message(req_run)
    assert resp_run is not None
    contents_run = resp_run["result"]["contents"]
    assert contents_run[0]["uri"] == f"aaw://runs/{test_run_id}/state"
    data_run = json.loads(contents_run[0]["text"])
    assert data_run["run_id"] == test_run_id
    assert data_run["task"]["run_id"] == test_run_id
    assert data_run["state"]["status"] == "RUNNING"
    assert len(data_run["iterations"]) == 1

    # 3. Unknown URI error
    req_bad = {
        "jsonrpc": "2.0",
        "id": "res-read-bad",
        "method": "resources/read",
        "params": {"uri": "aaw://unknown/resource"},
    }
    resp_bad = server.handle_message(req_bad)
    assert resp_bad is not None
    assert resp_bad["error"]["code"] == -32602


# ── 6. Stdio Transport Simulation ───────────────────────────────────────────


def test_serve_stdio_simulation() -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    ]
    raw_input = "\n".join(json.dumps(m) for m in messages) + "\n\n"  # includes blank line to test skip

    stdin = io.StringIO(raw_input)
    stdout = io.StringIO()

    exit_code = mcp.serve_stdio(stdin=stdin, stdout=stdout)
    assert exit_code == 0

    output_lines = [line.strip() for line in stdout.getvalue().splitlines() if line.strip()]
    # Notification does not produce an output line, so 4 input lines produce 3 output lines
    assert len(output_lines) == 3

    r1 = json.loads(output_lines[0])
    assert r1["id"] == 1
    assert r1["result"]["serverInfo"]["name"] == "aaw-mcp"

    r2 = json.loads(output_lines[1])
    assert r2["id"] == 2
    assert r2["result"] == {}

    r3 = json.loads(output_lines[2])
    assert r3["id"] == 3
    assert "resources" in r3["result"]


# ── 7. Loopback HTTP Transport ───────────────────────────────────────────────


def test_serve_http_transport() -> None:
    test_token = "mcp_test_secret_token_12345"
    # Bind to port 0 for dynamic ephemeral port allocation (avoids port collisions)
    http_server = mcp.create_http_server(host="127.0.0.1", port=0, auth_token=test_token)
    assigned_port = http_server.server_address[1]

    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{assigned_port}"
    auth_headers = {
        "Content-Type": "application/json",
        "X-AAW-Token": test_token,
        "Host": f"127.0.0.1:{assigned_port}",
    }

    try:
        # GET /ping (public healthcheck)
        with urllib.request.urlopen(f"{base_url}/ping", timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["status"] == "ok"
            assert data["server"] == "aaw-mcp"

        # 1. Negative Test: POST /mcp without token -> 401 Unauthorized
        ping_payload = json.dumps({"jsonrpc": "2.0", "id": "http-ping-noauth", "method": "ping"}).encode("utf-8")
        req_unauth = urllib.request.Request(
            f"{base_url}/mcp", data=ping_payload, headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{assigned_port}"}
        )
        with pytest.raises(urllib.error.HTTPError) as exc_unauth:
            urllib.request.urlopen(req_unauth, timeout=5)
        assert exc_unauth.value.code == 401

        # 2. Negative Test: POST /mcp with wrong Host header -> 403 Forbidden
        req_bad_host = urllib.request.Request(
            f"{base_url}/mcp",
            data=ping_payload,
            headers={"Content-Type": "application/json", "X-AAW-Token": test_token, "Host": "evil.attacker.com"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_host:
            urllib.request.urlopen(req_bad_host, timeout=5)
        assert exc_host.value.code == 403

        # 3. Negative Test: POST /mcp with untrusted Origin header -> 403 Forbidden
        req_bad_origin = urllib.request.Request(
            f"{base_url}/mcp",
            data=ping_payload,
            headers={**auth_headers, "Origin": "https://attacker.example.com"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_origin:
            urllib.request.urlopen(req_bad_origin, timeout=5)
        assert exc_origin.value.code == 403

        # 4. POST /mcp with ping request and X-AAW-Token -> 200 OK
        req = urllib.request.Request(
            f"{base_url}/mcp", data=ping_payload, headers=auth_headers
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["id"] == "http-ping-noauth"
            assert data["result"] == {}

        # 5. POST /mcp with Authorization Bearer header -> 200 OK
        bearer_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {test_token}",
            "Host": f"127.0.0.1:{assigned_port}",
        }
        req_bearer = urllib.request.Request(
            f"{base_url}/mcp", data=ping_payload, headers=bearer_headers
        )
        with urllib.request.urlopen(req_bearer, timeout=5) as resp:
            assert resp.status == 200

        # 6. POST /mcp with notification -> 204 No Content
        notif_payload = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8")
        req_notif = urllib.request.Request(
            f"{base_url}/mcp", data=notif_payload, headers=auth_headers
        )
        with urllib.request.urlopen(req_notif, timeout=5) as resp:
            assert resp.status == 204

        # 7. POST /mcp with tools/list
        tools_payload = json.dumps({"jsonrpc": "2.0", "id": "http-tools", "method": "tools/list"}).encode("utf-8")
        req_tools = urllib.request.Request(
            f"{base_url}/mcp", data=tools_payload, headers=auth_headers
        )
        with urllib.request.urlopen(req_tools, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert "tools" in data["result"]

        # 8. Negative Test: Malformed run_id in tool call
        bad_run_call = json.dumps({
            "jsonrpc": "2.0",
            "id": "http-bad-run",
            "method": "tools/call",
            "params": {"name": "aaw_run_status", "arguments": {"run_id": "../../../etc/passwd"}}
        }).encode("utf-8")
        req_bad_run = urllib.request.Request(
            f"{base_url}/mcp", data=bad_run_call, headers=auth_headers
        )
        with urllib.request.urlopen(req_bad_run, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["result"]["isError"] is True
            assert "Invalid run_id format" in data["result"]["content"][0]["text"]

        # 9. 404 for unknown endpoints
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/unknown_endpoint", timeout=5)
        assert exc_info.value.code == 404

    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=2.0)
