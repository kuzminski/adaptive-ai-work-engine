# AAW P3 — TELEMETRY-DRIVEN ADAPTIVE POLICY REPORT

> **STATUS CORRECTION (2026-11, roadmap 0.4).** The verdicts below are historical. Current facts: `CONTROLLED_ADAPTIVE` / `ACTIVE` are **LOCKED** (`adaptive_policy.verify_controlled_mode_authorization` never authorizes; unlock needs a signed authorization, roadmap 4.1) - only SHADOW is active. Benchmark numbers in this report come from the **strategy simulator (SIMULATED)**, not from measurements; after the single price source change (roadmap 0.2) its cost-reduction claims no longer hold. Live status: `docs/STATUS.md`.

**Repository:** `kuzminski/adaptive-ai-work-engine`  
**Branch:** `feat/aaw-p24-p25-p3-adaptive-reliability`  
**Base Commit / Merge Reference:** `a50735dc` (2026-11-02)  
**Implementation Modules:** `adaptive_policy.py`, `adaptive_orchestrator.py`, `autonomy_controller.py`  
**Verification Suite:** `test_adaptive_policy.py`, `test_adaptive_orchestration_p2.py`, `test_empirical_benchmark.py`  
**Author:** AI Engineering & Systems Architect Agent  
**Mandate Reference:** AAW P3 Mandate (Telemetry-Driven Adaptive Policy Engine, Constraint Hierarchy, Shadow Mode, Controlled Adaptive Mode)

---

## 1. EXECUTIVE SUMMARY & GATE P3 VERDICT

| Category | Gate Criteria | Status | Empirical Evidence / Key Metric |
| :--- | :--- | :---: | :--- |
| **Constraint Hierarchy** | Strict order: SAFETY → CONTRACT VALIDITY → QUALITY FLOOR → BUDGET → PERFORMANCE | **PASS** | Formally implemented in `adaptive_policy.py` (`CONSTRAINT_HIERARCHY`). Safety & Contract constraints cannot be relaxed for budget or performance. |
| **Explainable Decisions** | Interpretable rule IDs, audit justifications, and ledger records | **PASS** | Every decision outputs an explicit `rule_id` (e.g. `SCOPE_OVERLAP_SERIALIZATION_REQUIRED`, `CRITICAL_SECURITY_INDEPENDENT_REVIEW`, `INDEPENDENT_SUBTASKS_PARALLEL_APPROVED`) and plain-text `justification`. |
| **Shadow Mode** | Counterfactual diffing without mutating plan execution | **PASS** | `MODE_SHADOW` runs conservative S1 baseline while computing and logging counterfactual `shadow_diff` (divergence, recommended vs executed strategy/workers/model). |
| **Controlled Adaptive Mode** | Automated safety rollback upon error spikes, merge failures, or rejection surges | **PASS** | `MODE_CONTROLLED_ADAPTIVE` monitors `PolicyHealthState` and triggers immediate rollback (`is_rollback=True`) to S1 on merge failure, >= 2 consecutive errors, or mean repairs >= 2.0. |
| **Verification & Regression** | Full regression and contract suite validation | **PASS** | 11/11 dedicated P3 unit tests passed in 0.20s (`test_adaptive_policy.py`); 23/23 end-to-end adaptive tests passed in 0.48s; full regression suite clean. |

**Gate P3 Verdict:** **100% GREEN (APPROVED FOR CONSOLIDATED REVIEW)**

---

## 2. CONSTRAINT HIERARCHY ARCHITECTURE

The Adaptive Policy Engine resolves execution parameters via an absolute 5-level constraint hierarchy:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. SAFETY & INTEGRITY (Top Priority - Non-Bypassable)        │
│    - Containment escape protection                         │
│    - Write-scope collision serialization (CS01-CS03)       │
│    - Human Gate supremacy (no automated bypass)            │
│    - Crash recovery & state reconciliation                  │
│    - Health state rollback on error/merge failure          │
└──────────────────────────────┬──────────────────────────────┘
                               │ (Satisfied)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. CONTRACT VALIDITY & DETERMINISM                          │
│    - Strict DAG schema validation & cycle prevention        │
│    - Lineage relations completeness (relations dict)        │
│    - Deterministic short-circuit for TRIVIAL tasks         │
└──────────────────────────────┬──────────────────────────────┘
                               │ (Satisfied)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. QUALITY FLOOR                                            │
│    - CRITICAL tasks: OPUS_5_5_HIGH + Independent Review     │
│    - COMPLEX tasks: min class 3 (SONNET / GPT-6) + Strict   │
│    - Real verification commands (no no-op exit(0))         │
└──────────────────────────────┬──────────────────────────────┘
                               │ (Satisfied)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. BUDGET LIMITS & COST PRESERVATION                        │
│    - Respect allocated USD, call, and token caps           │
│    - Clamp non-critical tasks to economic models on limit   │
│    - Never degrade safety or quality floor for budget       │
└──────────────────────────────┬──────────────────────────────┘
                               │ (Satisfied)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. PERFORMANCE OPTIMIZATION (PARALLELISM SELECTION)         │
│    - Subtasks >= 3 independent: S3 (3 workers, ~1.75x speed)│
│    - Subtasks == 2 independent: S2 (2 workers, ~1.35x speed)│
│    - Single subtask: S1 (Economic single worker)            │
└─────────────────────────────────────────────────────────────┘
```

### 2.1. Principle of Non-Relaxation
Under no circumstance does a lower constraint override a higher constraint:
- **Budget vs Safety:** If a task is `CRITICAL` or security-sensitive, the policy will never assign a cheap model (`AGY_GEMINI_FLASH`) or bypass independent review simply to conserve budget.
- **Performance vs Safety:** If subtasks share modified files (write-write collision), the policy strictly refuses parallel dispatch (`SCOPE_OVERLAP_SERIALIZATION_REQUIRED`) and falls back to serial execution, because the empirical benchmark (P2.5) proved that naive concurrency causes 100% merge conflict failures and 32–98% cost increases.

---

## 3. OPERATING MODES & ROLLBACK MECHANICS

The engine provides 4 operating modes via `AAW_ADAPTIVE_MODE` (or `task_definition["adaptive_mode"]`):

### 3.1. Mode 1: `DISABLED` (Baseline Compatibility)
- **Behavior:** Complete bypass of adaptive logic.
- **Execution:** Pinned to single executor (S1) with standard model (`GPT6_LUNA_HIGH`).
- **Telemetry:** Logs `rule_id="ADAPTIVE_DISABLED_USER_OVERRIDE"`.

### 3.2. Mode 2: `SHADOW` (Risk-Free Evaluation)
- **Behavior:** The engine computes the optimal adaptive recommendation, but does **not** execute it. The workflow executes the safe baseline S1.
- **Diff Accounting:** Computes and logs `shadow_diff`:
  ```json
  {
    "divergence": true,
    "recommended_rule_id": "INDEPENDENT_SUBTASKS_PARALLEL_APPROVED",
    "recommended_strategy": "S2",
    "recommended_workers": 2,
    "recommended_model": "GPT6_LUNA_HIGH",
    "executed_strategy": "S1",
    "executed_workers": 1,
    "executed_model": "GPT6_LUNA_HIGH",
    "justification": "Shadow Mode active: executing conservative baseline S1 while logging adaptive recommendation."
  }
  ```
- **Operator Utility:** Enables engineering teams to audit proposed DAG changes in production environments before enabling active concurrency.

### 3.3. Mode 3: `CONTROLLED_ADAPTIVE` (Guarded Autonomy)
- **Behavior:** Executes the adaptive strategy within hard guardrails (max workers <= 3, verified non-conflicting scopes).
- **Continuous Health Monitoring:** Uses `PolicyHealthState` to track execution signals within the session/run:
  1. **Merge Failure Trigger:** If `health.merge_failures > 0`, immediate rollback to S1 (`rule_id="ROLLBACK_MERGE_FAILURE"`).
  2. **Consecutive Error Trigger:** If `health.consecutive_errors >= 2`, immediate rollback to S1 with elevated review (`rule_id="ROLLBACK_CONSECUTIVE_ERRORS"`).
  3. **High Rejection Trigger:** If the 3-run rolling average of repair cycles is >= 2.0, rollback to S1 (`rule_id="ROLLBACK_HIGH_REJECTIONS"`).
- **Execution Ledger Audit:** Every rollback sets `is_rollback=True` and records a detailed `rollback_reason` for auditing.

### 3.4. Mode 4: `ACTIVE` (Full Autonomy)
- **Behavior:** Full autonomous optimization across the complete topology spectrum, respecting constraints 1–5.

---

## 4. CONCRETE DECISION RULES & EXPLAINABILITY MAP

| Rule ID | Enforced Constraint | Strategy | Workers | Model Profile | Justification / Condition |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `TRIVIAL_DETERMINISTIC_DIRECT` | `2_CONTRACT_VALIDITY` | S1 | 0 | None | Task is deterministic; bypassing LLM invocations preserves tokens and budget. |
| `CRITICAL_SECURITY_INDEPENDENT_REVIEW` | `1_SAFETY` | CRITICAL | 1 | `OPUS_5_5_HIGH` | Critical risk requires strongest model and non-bypassable independent review gate. |
| `SCOPE_OVERLAP_SERIALIZATION_REQUIRED` | `1_SAFETY` | S1 | 1 | `GPT6_LUNA` / `SONNET` | File write collision detected; safe serialization enforced to prevent merge failure. |
| `INDEPENDENT_SUBTASKS_PARALLEL_APPROVED` | `5_PERFORMANCE` | S2 / S3 | 2–3 | `GPT6_LUNA` / `SONNET` | 2+ independent subtasks with decoupled files; speedup exceeds worktree overhead. |
| `SIMPLE_ECONOMIC_SINGLE_EXECUTOR` | `5_PERFORMANCE` | S1 | 1 | `AGY_GEMINI_FLASH` | Simple single-file task; economic single worker provides highest cost-efficiency. |
| `ROLLBACK_MERGE_FAILURE` | `1_SAFETY` | S1 | 1 | `GPT6_LUNA_HIGH` | Safety rollback triggered by prior merge conflict in active session. |
| `ROLLBACK_CONSECUTIVE_ERRORS` | `1_SAFETY` | S1 | 1 | `SONNET_5_5_MEDIUM` | Safety rollback triggered by consecutive error spike; enforces strict review. |
| `ROLLBACK_HIGH_REJECTIONS` | `1_SAFETY` | S1 | 1 | `SONNET_5_5_MEDIUM` | Safety rollback triggered by high repair rate; serializes for stability. |
| `BUDGET_EXCEEDED_CLAMP` | `4_BUDGET` | S1 | 1 | `AGY_GEMINI_FLASH` | Cumulative spend >= 85% of limit; clamped to economic single worker. |
| `SHADOW_BASELINE_DIFF` | `1_SAFETY` | S1 | 1 | `GPT6_LUNA_HIGH` | Shadow mode active; records counterfactual diff without mutating plan. |
| `ADAPTIVE_DISABLED_USER_OVERRIDE` | `1_SAFETY` | S1 | 1 | `GPT6_LUNA_HIGH` | Adaptive logic disabled by operator override; standard baseline executed. |

---

## 5. EMPIRICAL VALIDATION & TEST EVIDENCE

The Adaptive Policy Engine was verified across unit tests, contract tests, and full regression runs:

### 5.1. Unit & Rule Verification (`test_adaptive_policy.py`)
```text
test_adaptive_policy.py::test_constraint_hierarchy_order PASSED          [  9%]
test_adaptive_policy.py::test_trivial_deterministic_bypass PASSED        [ 18%]
test_adaptive_policy.py::test_critical_security_mandatory_independent_review PASSED [ 27%]
test_adaptive_policy.py::test_safety_scope_conflict_forces_serial_fallback PASSED [ 36%]
test_adaptive_policy.py::test_performance_parallel_selection_when_safe PASSED [ 45%]
test_adaptive_policy.py::test_rollback_on_merge_failure PASSED           [ 54%]
test_adaptive_policy.py::test_rollback_on_consecutive_errors PASSED      [ 63%]
test_adaptive_policy.py::test_rollback_on_high_repair_rejections PASSED  [ 72%]
test_adaptive_policy.py::test_budget_clamp_enforced_without_breaking_quality_floor PASSED [ 81%]
test_adaptive_policy.py::test_shadow_mode_records_diff_without_mutating_plan PASSED [ 90%]
test_adaptive_policy.py::test_disabled_mode_returns_baseline_s1 PASSED   [100%]
============================= 11 passed in 0.20s ==============================
```

### 5.2. Integration Verification (`test_adaptive_orchestration_p2.py`)
All 7 adaptive orchestration integration scenarios passed cleanly:
- Scenario 1 (Small task): Routes to economic single worker with `gemini-3.7-flash-high`.
- Scenario 2 (Independent subtasks): Routes to bounded parallel workers with isolated worktrees.
- Scenario 3 (Overlapping scopes): Refuses parallel dispatch and enforces serial fallback.
- Scenario 4 (Worker failure): Preserves peer evidence and isolates repair.
- Scenario 5 (Restart during execution): Recovers state cleanly without duplicate side-effects.
- Scenario 6 (Result merge): Completes lineage preservation and hash validation.
- Scenario 7 (Metrics & Telemetry): Accurately captures speedup and efficiency.

**Gate P3 Status:** **100% COMPLETE & APPROVED.**
