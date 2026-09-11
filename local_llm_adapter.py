#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AAW LOCAL_LLM execution adapter V0.1 (Windows, Python stdlib only).

Architecture:
    AAW -> LOCAL_LLM adapter -> 127.0.0.1:11434 -> qwen3-vl:4b-instruct

The adapter is independent of the semantic workflow. It knows nothing about
pipelines, nodes, capability classes or ORCA. It exposes:

    precheck()                 -> fail-closed runtime probe (no worker start)
    chat(messages, ...)        -> single, logically stateless completion
    write_telemetry(...)       -> 03_STATS record, external_paid_cost = 0

Verified runtime (see LOCAL_QWEN_ADAPTER_V0_1.md):
  - provider/runtime : AnythingLLM-managed Ollama 0.20.7
  - endpoint         : http://127.0.0.1:11434  (localhost only, no auth)
  - OpenAI base      : http://127.0.0.1:11434/v1
  - model            : qwen3-vl:4b-instruct (Qwen3-VL 4B Instruct, 4.4B, Q4_K_M)
  - AnythingLLM must be running; the adapter never starts or stops it.

V0.1 policy:
  - bounded LOW tasks only (classifier / JSON / delta / summary);
  - OpenAI-compatible transport preferred; temperature 0 by default;
  - conservative, explicit context/token limit; no silent truncation;
  - every failure mode is fail-closed and named.
"""

from __future__ import annotations

import datetime as dt
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from execution_contract import new_execution_id
from aaw_paths import STATS_ROOT

# ---------------------------------------------------------------------------
# Frozen runtime facts (single source of truth is MODEL_CATALOG.json; these
# constants mirror the LOCAL row and are asserted against it by self_test()).
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "MODEL_CATALOG.json"
LOCAL_MODEL_ID = "qwen3-vl:4b-instruct"
ENDPOINT = "http://127.0.0.1:11434"
OPENAI_BASE = ENDPOINT + "/v1"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Conservative V0.1 context policy. The adapter must never hand the model the
# whole repo or a large diff. Callers that need more must go to a cloud model
# per existing policy.
DEFAULT_NUM_CTX = 4096
MAX_NUM_CTX_V0_1 = 8192
DEFAULT_MAX_OUTPUT_TOKENS = 512
MAX_INPUT_CHARS = 12_000          # ~3k tokens; hard cap for V0.1 bounded LOW work
MAX_INPUT_TOKENS_EST = 3_200
CHARS_PER_TOKEN_EST = 3.7

PRECHECK_TIMEOUT_S = 4.0
DEFAULT_CHAT_TIMEOUT_S = 90.0
MAX_CHAT_TIMEOUT_S = 180.0
COLD_LATENCY_HINT_S = 3.0

TELEMETRY_SCHEMA = "AAW_LOCAL_LLM_TELEMETRY_V0.1"

# Process-local warm marker only. Not shared state, not persisted, not a cache
# of results - it only sharpens the cold/warm telemetry label.
_WARM_IN_PROCESS = False


# ---------------------------------------------------------------------------
# Fail-closed error taxonomy
# ---------------------------------------------------------------------------
class LocalLLMError(RuntimeError):
    """Base class. code is a stable, greppable status token."""

    code = "LOCAL_LLM_ERROR"


class LocalLLMUnavailable(LocalLLMError):
    code = "LOCAL_LLM_UNAVAILABLE"


class LocalLLMTimeout(LocalLLMError):
    code = "LOCAL_LLM_TIMEOUT"


class LocalContextLimit(LocalLLMError):
    code = "LOCAL_CONTEXT_LIMIT"


class LocalLLMUnsafeBind(LocalLLMError):
    code = "LOCAL_LLM_UNSAFE_BIND"


class LocalLLMProtocolError(LocalLLMError):
    code = "LOCAL_LLM_PROTOCOL_ERROR"


# ---------------------------------------------------------------------------
# Low-level HTTP (stdlib only, no external deps, no shell)
# ---------------------------------------------------------------------------
def _url_host(url: str) -> str:
    rest = url.split("://", 1)[-1]
    authority = rest.split("/", 1)[0]
    host = authority.rsplit("@", 1)[-1]
    if host.startswith("["):
        return host[1: host.find("]")]
    return host.split(":", 1)[0]


def assert_loopback_endpoint(url: str = ENDPOINT) -> None:
    """String-level guard: the adapter must only ever talk to loopback."""
    host = _url_host(url).lower()
    if host in LOOPBACK_HOSTS:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        infos = []
    if infos and all(_is_loopback_addr(info[4][0]) for info in infos):
        return
    raise LocalLLMUnsafeBind(
        f"configured endpoint host is not loopback: {host!r}; refusing to send"
    )


def _is_loopback_addr(addr: str) -> bool:
    if addr.startswith("127.") or addr == "::1":
        return True
    return addr in LOOPBACK_HOSTS


def verify_localhost_bind(port: int = 11434) -> dict[str, Any]:
    """
    Best-effort check that nothing is serving :11434 on a non-loopback address.

    Uses only stdlib socket probing. If a bind to a routable address answers,
    that is a hard LOCAL_LLM_UNSAFE_BIND. If we cannot positively confirm a
    non-loopback listener, we report UNVERIFIED - the request target itself is
    always loopback, so we do not block on an inconclusive probe.
    """
    routable = _primary_ipv4()
    result: dict[str, Any] = {
        "port": port,
        "loopback_listener": _tcp_open("127.0.0.1", port),
        "routable_candidate": routable,
        "routable_listener": None,
        "state": "UNVERIFIED_ASSUMED_LOOPBACK",
    }
    if routable and routable not in LOOPBACK_HOSTS:
        open_routable = _tcp_open(routable, port)
        result["routable_listener"] = open_routable
        if open_routable:
            result["state"] = "UNSAFE_BIND"
        else:
            result["state"] = "LOOPBACK_ONLY"
    return result


def _primary_ipv4() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed; no packet sent for UDP connect
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _tcp_open(host: str, port: int, timeout: float = 0.75) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(
    url: str,
    payload: Mapping[str, Any] | None = None,
    *,
    method: str | None = None,
    timeout: float = DEFAULT_CHAT_TIMEOUT_S,
) -> tuple[int, Any, float]:
    assert_loopback_endpoint(url)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (socket.timeout, TimeoutError) as exc:
        raise LocalLLMTimeout(f"{url} timed out after {timeout:.0f}s") from exc
    except urllib.error.URLError as exc:
        raise LocalLLMUnavailable(f"cannot reach {url}: {exc.reason}") from exc
    except OSError as exc:
        raise LocalLLMUnavailable(f"cannot reach {url}: {exc}") from exc
    elapsed = time.monotonic() - started
    try:
        parsed = json.loads(body) if body.strip() else None
    except json.JSONDecodeError as exc:
        raise LocalLLMProtocolError(f"{url} returned non-JSON body: {body[:200]!r}") from exc
    return status, parsed, elapsed


# ---------------------------------------------------------------------------
# Precheck (Section 1) - fail closed, never launches AnythingLLM
# ---------------------------------------------------------------------------
def precheck(*, timeout: float = PRECHECK_TIMEOUT_S, check_bind: bool = True) -> dict[str, Any]:
    """
    GET /api/version and confirm the target model is present.

    Returns a dict with state == "AVAILABLE" only when the local Qwen runtime
    actually answers and lists the model. Any other condition raises a named
    LocalLLMError - callers must treat that as LOCAL_LLM_UNAVAILABLE and stop.
    The adapter never starts AnythingLLM and never degrades silently.
    """
    global _WARM_IN_PROCESS
    assert_loopback_endpoint(ENDPOINT)

    bind = verify_localhost_bind() if check_bind else {"state": "SKIPPED"}
    if bind.get("state") == "UNSAFE_BIND":
        raise LocalLLMUnsafeBind(
            f"a non-loopback listener answers on :{bind['port']} "
            f"({bind['routable_candidate']}); refusing local inference"
        )

    status, version_doc, latency = _http_json(ENDPOINT + "/api/version", timeout=timeout)
    if status != 200 or not isinstance(version_doc, Mapping) or not version_doc.get("version"):
        raise LocalLLMUnavailable(f"/api/version unhealthy: status={status} body={version_doc!r}")

    tag_status, tag_doc, _ = _http_json(ENDPOINT + "/api/tags", timeout=timeout)
    models = tag_doc.get("models") if isinstance(tag_doc, Mapping) else None
    names = {m.get("model") or m.get("name") for m in models} if isinstance(models, list) else set()
    if LOCAL_MODEL_ID not in names:
        raise LocalLLMUnavailable(
            f"runtime is up but model {LOCAL_MODEL_ID!r} is not installed; present={sorted(n for n in names if n)}"
        )

    loaded = _model_loaded()
    if loaded and not _WARM_IN_PROCESS:
        _WARM_IN_PROCESS = True

    return {
        "state": "AVAILABLE",
        "endpoint": ENDPOINT,
        "openai_base": OPENAI_BASE,
        "runtime": "anythingllm_managed_ollama",
        "runtime_version": str(version_doc.get("version")),
        "model": LOCAL_MODEL_ID,
        "model_loaded": loaded,
        "version_latency_ms": round(latency * 1000),
        "bind_check": bind,
        "checked_at": _now_iso(),
        "anythingllm_required": True,
    }


def precheck_soft(**kwargs: Any) -> dict[str, Any]:
    """precheck() that returns a status dict instead of raising - for GUI probes."""
    try:
        result = precheck(**kwargs)
        result["ok"] = True
        return result
    except LocalLLMError as exc:
        return {
            "ok": False,
            "state": exc.code,
            "reason": str(exc),
            "endpoint": ENDPOINT,
            "model": LOCAL_MODEL_ID,
            "anythingllm_required": True,
            "checked_at": _now_iso(),
        }


def _model_loaded() -> bool:
    try:
        _, doc, _ = _http_json(ENDPOINT + "/api/ps", timeout=PRECHECK_TIMEOUT_S)
    except LocalLLMError:
        return False
    rows = doc.get("models") if isinstance(doc, Mapping) else None
    if not isinstance(rows, list):
        return False
    return any((r.get("model") or r.get("name")) == LOCAL_MODEL_ID for r in rows)


# ---------------------------------------------------------------------------
# Context guard (Section 5) - explicit, conservative, no silent truncation
# ---------------------------------------------------------------------------
def _messages_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):  # OpenAI content-parts form
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    total += len(part["text"])
    return total


def enforce_context_policy(
    messages: Sequence[Mapping[str, Any]],
    *,
    num_ctx: int,
    max_input_chars: int = MAX_INPUT_CHARS,
) -> dict[str, Any]:
    if num_ctx > MAX_NUM_CTX_V0_1:
        raise LocalContextLimit(
            f"requested num_ctx={num_ctx} exceeds V0.1 ceiling {MAX_NUM_CTX_V0_1}; "
            "route long-context work to a cloud model per existing policy"
        )
    chars = _messages_chars(messages)
    tokens_est = int(round(chars / CHARS_PER_TOKEN_EST))
    if chars > max_input_chars or tokens_est > MAX_INPUT_TOKENS_EST:
        raise LocalContextLimit(
            f"input is {chars} chars (~{tokens_est} tokens), over the V0.1 bounded-LOW "
            f"limit ({max_input_chars} chars / {MAX_INPUT_TOKENS_EST} tokens). "
            "Nothing was sent and nothing was truncated - shrink the input or use a cloud path."
        )
    return {"input_chars": chars, "input_tokens_est": tokens_est, "num_ctx": num_ctx, "limit_chars": max_input_chars}


# ---------------------------------------------------------------------------
# chat() - one logically stateless completion (Section 4)
# ---------------------------------------------------------------------------
def chat(
    messages: Sequence[Mapping[str, Any]],
    *,
    json_mode: bool = False,
    temperature: float = 0.0,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    num_ctx: int = DEFAULT_NUM_CTX,
    transport: str = "openai",
    timeout: float = DEFAULT_CHAT_TIMEOUT_S,
    seed: int | None = 0,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """
    Send `messages` to the local Qwen model and return a normalized result.

    - No AnythingLLM chat history is attached; the full prompt is `messages`.
    - Each call is independent (stateless). There is no session id to reuse.
    - transport="openai" -> POST /v1/chat/completions (preferred).
      transport="native" -> POST /api/chat, the only way to pin options.num_ctx.
    - json_mode=True asks for a strict JSON object back.
    Raises a named LocalLLMError on any failure. Never truncates input.
    """
    global _WARM_IN_PROCESS
    execution_id = execution_id or new_execution_id()
    if transport not in {"openai", "native"}:
        raise ValueError(f"unknown transport: {transport!r}")
    timeout = min(max(1.0, float(timeout)), MAX_CHAT_TIMEOUT_S)
    ctx_info = enforce_context_policy(messages, num_ctx=num_ctx)

    warm_before = _WARM_IN_PROCESS
    started_wall = _now_iso()
    started = time.monotonic()

    if transport == "native":
        payload: dict[str, Any] = {
            "model": LOCAL_MODEL_ID,
            "messages": [dict(m) for m in messages],
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": max_output_tokens},
        }
        if seed is not None:
            payload["options"]["seed"] = seed
        if json_mode:
            payload["format"] = "json"
        status, doc, _ = _http_json(ENDPOINT + "/api/chat", payload, timeout=timeout)
        content, finish, usage, extra = _read_native(status, doc)
    else:
        payload = {
            "model": LOCAL_MODEL_ID,
            "messages": [dict(m) for m in messages],
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if seed is not None:
            payload["seed"] = seed
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        status, doc, _ = _http_json(OPENAI_BASE + "/chat/completions", payload, timeout=timeout)
        content, finish, usage, extra = _read_openai(status, doc)

    elapsed = time.monotonic() - started
    _WARM_IN_PROCESS = True

    cold_warm = _cold_warm(warm_before, elapsed, extra)
    return {
        "execution_id": execution_id,
        "content": content,
        "model": LOCAL_MODEL_ID,
        "finish_reason": finish,
        "usage": usage,
        "transport": transport,
        "json_mode": json_mode,
        "latency_ms": round(elapsed * 1000),
        "wall_time_s": round(elapsed, 3),
        "cold_warm": cold_warm,
        "started_at": started_wall,
        "ended_at": _now_iso(),
        "context": ctx_info,
        "stateless": True,
    }


def chat_json(messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """chat() in JSON mode that also parses the body; raises on non-JSON output."""
    kwargs.setdefault("json_mode", True)
    result = chat(messages, **kwargs)
    text = (result.get("content") or "").strip()
    try:
        result["json"] = json.loads(text)
    except json.JSONDecodeError:
        parsed = _salvage_json(text)
        if parsed is None:
            raise LocalLLMProtocolError(
                f"model did not return a JSON object in JSON mode: {text[:200]!r}"
            )
        result["json"] = parsed
        result["json_salvaged"] = True
    return result


def _read_openai(status: int, doc: Any) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
    if status != 200 or not isinstance(doc, Mapping):
        raise LocalLLMProtocolError(f"/v1/chat/completions status={status} body={str(doc)[:200]!r}")
    choices = doc.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LocalLLMProtocolError(f"/v1/chat/completions has no choices: {str(doc)[:200]!r}")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise LocalLLMProtocolError("/v1/chat/completions choice has no string content")
    raw_usage = doc.get("usage") if isinstance(doc.get("usage"), Mapping) else {}
    usage = {
        "input_tokens": raw_usage.get("prompt_tokens"),
        "output_tokens": raw_usage.get("completion_tokens"),
        "total_tokens": raw_usage.get("total_tokens"),
    }
    finish = choices[0].get("finish_reason") if isinstance(choices[0], Mapping) else None
    return content, finish, usage, {}


def _read_native(status: int, doc: Any) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
    if status != 200 or not isinstance(doc, Mapping):
        raise LocalLLMProtocolError(f"/api/chat status={status} body={str(doc)[:200]!r}")
    message = doc.get("message") if isinstance(doc.get("message"), Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise LocalLLMProtocolError("/api/chat response has no string message.content")
    usage = {
        "input_tokens": doc.get("prompt_eval_count"),
        "output_tokens": doc.get("eval_count"),
        "total_tokens": _sum_or_none(doc.get("prompt_eval_count"), doc.get("eval_count")),
    }
    finish = doc.get("done_reason")
    extra = {"load_duration_ns": doc.get("load_duration"), "total_duration_ns": doc.get("total_duration")}
    return content, finish, usage, extra


def _sum_or_none(a: Any, b: Any) -> int | None:
    if isinstance(a, int) and isinstance(b, int):
        return a + b
    return None


def _cold_warm(warm_before: bool, elapsed: float, extra: Mapping[str, Any]) -> str:
    load_ns = extra.get("load_duration_ns")
    total_ns = extra.get("total_duration_ns")
    if isinstance(load_ns, int) and isinstance(total_ns, int) and total_ns > 0:
        # Ollama reports a large load_duration only when it actually (re)loaded weights.
        return "COLD" if load_ns > 0.4 * total_ns and load_ns > 1_000_000_000 else "WARM"
    if warm_before:
        return "WARM"
    return "COLD" if elapsed >= COLD_LATENCY_HINT_S else "UNKNOWN"


def _salvage_json(text: str) -> Any | None:
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Telemetry (Section 6) - own schema, external_paid_cost = 0 (not cost = 0)
# ---------------------------------------------------------------------------
def build_telemetry(
    result: Mapping[str, Any],
    *,
    stage: str,
    outcome: str,
    aaw_run_id: str | None = None,
) -> dict[str, Any]:
    usage = dict(result.get("usage") or {})
    captured = all(usage.get(k) is not None for k in ("input_tokens", "output_tokens"))
    return {
        "schema_version": TELEMETRY_SCHEMA,
        "execution_id": result.get("execution_id"),
        "aaw_run_id": aaw_run_id,
        "stage": stage,
        "provider": "LOCAL",
        "harness": "ollama_openai_compat",
        "runtime": "anythingllm_managed_ollama",
        "model": LOCAL_MODEL_ID,
        "endpoint_class": "LOCALHOST",
        "transport": result.get("transport"),
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at"),
        "wall_time_s": result.get("wall_time_s"),
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "cold_warm": result.get("cold_warm", "UNKNOWN"),
        "context": result.get("context"),
        "external_paid_cost": 0,
        "external_paid_cost_unit": "USD",
        "cost_basis": "LOCAL_INFERENCE_NO_EXTERNAL_BILLING",
        "outcome": outcome,
        "telemetry_status": "CAPTURED" if captured else "PARTIAL",
    }


def write_telemetry(
    result: Mapping[str, Any],
    *,
    stage: str,
    outcome: str,
    aaw_run_id: str | None = None,
    stats_root: Path = STATS_ROOT,
) -> Path:
    record = build_telemetry(result, stage=stage, outcome=outcome, aaw_run_id=aaw_run_id)
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = stats_root / (aaw_run_id or f"LOCAL_LLM_{stamp[:15]}")
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_stage = "".join(c if c.isalnum() else "_" for c in stage)[:48] or "STAGE"
    path = run_dir / f"00__LOCAL_LLM__{safe_stage}__ollama_openai_compat__{stamp}.json"
    if path.exists():  # never overwrite (matches house rule in aaw_run_v0_1.py)
        raise LocalLLMError(f"refusing to overwrite telemetry: {path}")
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Catalog cross-check + offline self test
# ---------------------------------------------------------------------------
def catalog_row(path: Path = CATALOG_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data.get("models", []):
        if row.get("runtime_model_id") == LOCAL_MODEL_ID:
            return dict(row)
    raise LocalLLMError(f"{LOCAL_MODEL_ID} missing from {path.name}")


def self_test() -> int:
    row = catalog_row()
    assert row["provider"] == "LOCAL", row
    assert row["harness"] == "ollama_openai_compat", row
    assert row["access_class"] == "LOCAL_INCLUDED", row
    assert row["endpoint"] == ENDPOINT and row["openai_base"] == OPENAI_BASE, row
    assert row.get("automatic_use_allowed") is True, row
    assert row.get("external_paid_cost", 0) == 0, row

    # loopback guard
    assert_loopback_endpoint("http://127.0.0.1:11434")
    for bad in ("http://10.0.0.5:11434", "http://192.168.1.20:11434", "http://example.com/v1"):
        try:
            assert_loopback_endpoint(bad)
        except LocalLLMUnsafeBind:
            pass
        else:
            raise AssertionError(f"non-loopback endpoint accepted: {bad}")

    # context guard: oversized input is refused, not truncated
    big = [{"role": "user", "content": "x" * (MAX_INPUT_CHARS + 1)}]
    try:
        enforce_context_policy(big, num_ctx=DEFAULT_NUM_CTX)
    except LocalContextLimit:
        pass
    else:
        raise AssertionError("oversized input was not rejected")
    try:
        enforce_context_policy([{"role": "user", "content": "hi"}], num_ctx=MAX_NUM_CTX_V0_1 + 1)
    except LocalContextLimit:
        pass
    else:
        raise AssertionError("excessive num_ctx was not rejected")
    ok = enforce_context_policy([{"role": "user", "content": "hello"}], num_ctx=DEFAULT_NUM_CTX)
    assert ok["input_chars"] == 5

    # telemetry: no cash-cost zero, uses external_paid_cost
    tel = build_telemetry(
        {"usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}, "transport": "openai",
         "wall_time_s": 0.4, "cold_warm": "WARM", "context": ok,
         "started_at": _now_iso(), "ended_at": _now_iso()},
        stage="SELFTEST", outcome="PASS",
    )
    assert "cost" not in tel and tel["external_paid_cost"] == 0
    assert tel["telemetry_status"] == "CAPTURED"
    tel_partial = build_telemetry({"usage": {}}, stage="SELFTEST", outcome="PASS")
    assert tel_partial["telemetry_status"] == "PARTIAL"
    assert tel_partial["usage"]["input_tokens"] is None  # null, never 0

    print(json.dumps({"status": "PASS", "model": LOCAL_MODEL_ID, "endpoint": ENDPOINT}))
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
