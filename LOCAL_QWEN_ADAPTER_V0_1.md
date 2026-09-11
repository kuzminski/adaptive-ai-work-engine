# AAW LOCAL_LLM Execution Adapter — V0.1

Status: **OPERATIONAL_WITH_LIMITATIONS** (2026-09-06)
Scope: first `LOCAL_LLM` execution adapter for Adaptive AI Work. Bounded LOW work only.

```
AAW  ->  LOCAL_LLM adapter  ->  127.0.0.1:11434  ->  qwen3-vl:4b-instruct
```

The adapter is **independent of the semantic workflow**. It has no knowledge of
pipelines, nodes, capability classes, ORCA, or the classifier. It exposes three
things: `precheck()`, `chat()` / `chat_json()`, and `write_telemetry()`.

---

## 1. Verified runtime

| item | value |
|---|---|
| Provider / runtime | AnythingLLM-managed Ollama |
| AnythingLLM Desktop | 1.16.1 (`%LOCALAPPDATA%\Programs\AnythingLLM`) |
| Ollama engine | 0.20.7 (`%APPDATA%\anythingllm-desktop\storage\engines\ollama\llm.exe`) |
| Endpoint | `http://127.0.0.1:11434` (loopback only, **no auth**) |
| OpenAI-compatible base | `http://127.0.0.1:11434/v1` |
| Model tag | `qwen3-vl:4b-instruct` — Qwen3-VL 4B Instruct |
| Parameters / quant | 4.4B / Q4_K_M (GGUF) |
| Model max context | 262144 |
| Internal provider id | `anythingllm_ollama` (AnythingLLM `.env` `LLM_PROVIDER`) |
| Direct smoke | `POST /api/generate` and `/v1/chat/completions` both return `AAW_LOCAL_QWEN_DIRECT_PASS` |

AnythingLLM **must be running**. `llm.exe` is a child of `AnythingLLM.exe`;
closing the app tears down `:11434`. The adapter never starts or stops
AnythingLLM (Section 13).

---

## 2. Files

| path | role |
|---|---|
| `05_AAW/local_llm_adapter.py` | the adapter (stdlib only: `urllib`, `socket`, `json`) |
| `05_AAW/test_local_llm_adapter.py` | 20 offline unit tests (no network) |
| `05_AAW/MODEL_CATALOG.json` | new `LOCAL` row (provider `LOCAL`, harness `ollama_openai_compat`, `access_class` `LOCAL_INCLUDED`) |
| `05_AAW/IMPLEMENTER_PROFILES.json` | `LOCAL_QWEN_FAST` / `_JSON` / `_DELTA` / `_SUMMARY` |
| `05_AAW/model_catalog.py` | `is_local_model()`, `local_profile_ids()`, `resolve_profile()` local branch |
| `05_AAW/CONTROL_CENTER/aaw_control_center.py` | `probe_local_llm_runtime()` + `ollama_openai_compat` harness in preflight |
| `05_AAW/CONTROL_CENTER/test_local_llm_runtime_integration.py` | 9 GUI-integration tests (adapter stubbed) |
| `05_AAW/LOCAL_LLM_SMOKES/local_qwen_smoke.py` | live smoke suite (Section 16) |
| `05_AAW/LOCAL_LLM_SMOKES/delta_review_smoke.py` | bounded delta-review probe (Section 10) |
| `05_AAW/LOCAL_LLM_SMOKES/classifier_challenger_luna_vs_qwen.py` | fixed comparison (Section 9) |

Not touched: `aaw_run_v0_1.py`, `workflow_runner.py`, ORCA, `MODEL_REGISTRY.json`,
the cheap-classifier prompt, `test_model_runtime_availability.py`.

---

## 3. Precheck (fail-closed)

`precheck()` — `GET /api/version` + `GET /api/tags`, ~4 s timeout. Returns
`state == "AVAILABLE"` only when the runtime answers **and** lists
`qwen3-vl:4b-instruct`. Any other condition raises a named error:

| error `.code` | when |
|---|---|
| `LOCAL_LLM_UNAVAILABLE` | connection refused / `/api/version` unhealthy / model not installed |
| `LOCAL_LLM_TIMEOUT` | runtime did not answer within the deadline |
| `LOCAL_LLM_UNSAFE_BIND` | a non-loopback listener answers on `:11434` |
| `LOCAL_LLM_PROTOCOL_ERROR` | non-200 / malformed response envelope |
| `LOCAL_CONTEXT_LIMIT` | input over the V0.1 bound (see §5) |

There is **no silent degradation**. Callers treat any raise as
`LOCAL_LLM_UNAVAILABLE` and stop. `precheck_soft()` returns a status dict
instead of raising, for GUI probes.

---

## 4. Request contract

- Transport `"openai"` (default) → `POST /v1/chat/completions`.
  Transport `"native"` → `POST /api/chat` (the only way to pin `options.num_ctx`).
- `temperature = 0`, `seed = 0` by default.
- `json_mode=True` → `response_format={"type":"json_object"}` (openai) or
  `format:"json"` (native). `chat_json()` also parses and **raises** on non-JSON.
- **No AnythingLLM chat history is attached.** The full prompt is `messages`.
  Every call is independent — there is no `sessionId` to reuse and none is sent.
- `max_output_tokens` default 512.

---

## 5. Context policy (conservative, explicit, no silent truncation)

| knob | V0.1 value |
|---|---|
| default `num_ctx` | 4096 |
| hard `num_ctx` ceiling | 8192 (`LOCAL_CONTEXT_LIMIT` above this) |
| max input | 12000 chars / ~3200 est. tokens |
| behaviour on overflow | **raise `LOCAL_CONTEXT_LIMIT`; nothing is sent, nothing is trimmed** |

Rationale: V0.1 is for bounded LOW tasks. The adapter must never be handed the
whole repo or a large diff. Work over the limit is the caller's problem — route
it to a cloud model per existing policy.

---

## 6. Telemetry

Own schema `AAW_LOCAL_LLM_TELEMETRY_V0.1`, written under
`03_STATS/<run_id>/00__LOCAL_LLM__<stage>__ollama_openai_compat__<ts>.json`
(never overwrites, matching the house rule in `aaw_run_v0_1.py`).

- `provider="LOCAL"`, `harness="ollama_openai_compat"`, `endpoint_class="LOCALHOST"`.
- `usage.input_tokens` / `output_tokens` = the values the endpoint returns, or
  **`null`** when absent — never `0` (Telemetry Contract rule).
- Cost: **`external_paid_cost: 0`** with `cost_basis:"LOCAL_INFERENCE_NO_EXTERNAL_BILLING"`.
  There is **no `cost` key** — the Telemetry Contract forbids labelling a
  reference figure as subscription cash cost, and here there is genuinely no
  external payment.
- `cold_warm` from `load_duration` (native) or a process-local warm marker +
  latency heuristic (openai).
- `telemetry_status`: `CAPTURED` when both token counts are present, else `PARTIAL`.

---

## 7. Safety

- The adapter only ever talks to a **loopback** URL. `assert_loopback_endpoint()`
  refuses any non-loopback host before a request is sent.
- `verify_localhost_bind()` (stdlib socket probe) checks that nothing serves
  `:11434` on a routable address. A routable listener → hard
  `LOCAL_LLM_UNSAFE_BIND` (BLOCKED). An inconclusive probe → `UNVERIFIED_ASSUMED_LOOPBACK`
  (not blocked — the request target is loopback regardless).
- V0.1 does **not** change the bind address, open a LAN port, or add an auth proxy.

---

## 8. Catalog / profile wiring

`MODEL_CATALOG.json` LOCAL row: `runtime_available: true` +
`runtime_available_policy: "DYNAMIC_PREFLIGHT"`. Static resolution succeeds; the
**adapter `precheck()` is the real runtime gate at call time**.
`resolve_profile()` returns `runtime_preflight_required: true` and
`not_implementer: true` for local presets.

The four presets are **human-selectable only**. They are **not** in
`MODEL_REGISTRY.active_bindings`, so the automatic capability→model resolver in
`aaw_run_v0_1.py` never picks them. They are excluded from the Control Center
IMPLEMENT/REVIEW/REPAIR node-binding pickers (`_node_binding_profiles()`), and
visible on **Settings → Models** with live runtime state.

| preset | suitable_for | use |
|---|---|---|
| `LOCAL_QWEN_FAST` | CLASSIFY / TRIAGE / SHORT_TRANSFORM | cheap classifier, task triage |
| `LOCAL_QWEN_JSON` | JSON_NORMALIZE / STRUCTURED_OUTPUT | JSON normalization |
| `LOCAL_QWEN_DELTA` | DELTA_REVIEW / BOUNDED_DIFF_REVIEW | small delta / bounded diff review |
| `LOCAL_QWEN_SUMMARY` | SUMMARY / HANDOFF_SUMMARY / EVIDENCE_SUMMARY | short handoff / evidence summaries |

Never a default for IMPLEMENT, ARCHITECT_STRONG, FULL_REVIEW. Never an
implementer in Simple mode.

---

## 9. Measured behaviour (2026-09-06, two runs)

| dimension | result |
|---|---|
| JSON-mode validity (12 structured prompts) | **12 / 12 (100%)** |
| six-key classifier JSON validity (17-case corpus) | **17 / 17 (100%)**, malformed 0 |
| deterministic pipeline match | **10 / 11 (91%)** |
| deterministic full six-field match | 2 / 11 (18%) |
| deterministic "correct behaviour" (right pipeline, no false escalation) | 3 / 11 (27%) |
| ambiguous-case escalation (must go to HUMAN_REQUIRED) | **6 / 6 (100%)** — equals recorded Luna baseline |
| delta-review structural validity | 3 / 3 (100%) |
| delta-review semantic accuracy | 2 / 3 |
| latency — cold | ~8–9 s (first call / after model reload) |
| latency — warm | ~7–14 s, median ~8–12 s |

**Systematic finding:** Qwen3-VL 4B is under-confident on the classifier prompt
(defaults to `confidence ≈ 0.86`, `ambiguity = medium`), so it would push many
deterministically-clean tasks to HUMAN_REQUIRED — over-escalation, not
misrouting. Format reliability is excellent; latency is ~10–30× the
Luna/`none` cheap classifier.

Challenger verdict: **`LOCAL_QWEN_CLASSIFIER_NEEDS_MORE_EVIDENCE`**.
The production classifier policy is **unchanged**.

---

## 10. Not in V0.1 (future)

- Dedicated "Advanced model picker" Local → Qwen entry in the GUI (presets are
  visible on Settings → Models; programmatic use via `model_catalog.resolve_profile`
  + the adapter works today).
- Larger local models over the same transport (`ollama pull` + a new catalog
  row; no workflow-runner change).
- Optional AnythingLLM launcher (separate task).
- Live Luna head-to-head: `classifier_challenger_luna_vs_qwen.py --with-luna-live`.
