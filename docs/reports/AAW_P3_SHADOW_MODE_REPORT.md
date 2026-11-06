# AAW P3 ADAPTIVE POLICY ENGINE: SHADOW MODE GATING REPORT

> **STATUS CORRECTION (2026-11, roadmap 0.4).** The verdicts below are historical. Current facts: `CONTROLLED_ADAPTIVE` / `ACTIVE` are **LOCKED** (`adaptive_policy.verify_controlled_mode_authorization` never authorizes; unlock needs a signed authorization, roadmap 4.1) - only SHADOW is active. Benchmark numbers in this report come from the **strategy simulator (SIMULATED)**, not from measurements; after the single price source change (roadmap 0.2) its cost-reduction claims no longer hold. Live status: `docs/STATUS.md`.

**Document Reference:** `AAW_P3_SHADOW_MODE_REPORT.md`  
**Base Commit Reference:** `a50735dc`  
**Execution Worktree:** `feat/aaw-p24-p25-p3-adaptive-reliability`  
**Status:** ENFORCED IN SHADOW MODE  
**Date:** 2026-11-02  

---

## 1. Governance Mandate & Operating Scope

Under the AAW P3 Governance Mandate, the Telemetry-Driven Adaptive Policy Engine is admitted **strictly in SHADOW mode** (`MODE_SHADOW`).

### 1.1 Invariant Guarantees
1. **Zero Plan Mutation in Production:** While in `MODE_SHADOW`, the runtime strictly executes the conservative baseline strategy (**S1: Single Executor Sequential**, baseline model `GPT6_LUNA_HIGH`, standard verification). No dynamic multi-worker DAGs or model escalations are triggered.
2. **Counterfactual Telemetry Capture:** For every incoming task, the policy evaluates what strategy it *would* have recommended, logging the justification, rule ID, and predicted benefits (`shadow_diff`).
3. **Controlled Mode Opt-in Requirement:** The active adaptive orchestration mode (`MODE_CONTROLLED_ADAPTIVE`) is locked behind an explicit configuration gate (`AAW_ADAPTIVE_MODE=CONTROLLED_ADAPTIVE`). It cannot be enabled automatically or by default until representative production telemetry is gathered and approved.

---

## 2. Policy Engine Architecture

The policy engine (`adaptive_policy.py`) enforces a strict, lexicographically ordered 5-layer constraint hierarchy:

```
  ┌────────────────────────────────────────────────────────┐
  │ 1. SAFETY & INTEGRITY                                  │
  │    - Merge failure / error spike rollback triggers     │
  │    - Security critical independent review gating       │
  │    - Scope conflict detection & serialization          │
  └───────────────────────────┬────────────────────────────┘
                              │
  ┌───────────────────────────▼────────────────────────────┐
  │ 2. CONTRACT VALIDITY & DETERMINISM                     │
  │    - Trivial tasks: 0-worker deterministic bypass      │
  │    - Required input/output schema contract checks      │
  └───────────────────────────┬────────────────────────────┘
                              │
  ┌───────────────────────────▼────────────────────────────┐
  │ 3. QUALITY FLOOR                                       │
  │    - Complex tasks: minimum Tier 4 (Sonnet 5.5)        │
  │    - Strict test verification requirement              │
  └───────────────────────────┬────────────────────────────┘
                              │
  ┌───────────────────────────▼────────────────────────────┐
  │ 4. BUDGET GOVERNANCE                                   │
  │    - Cumulative spend tracking                         │
  │    - Economic model clamping without breaching floor   │
  └───────────────────────────┬────────────────────────────┘
                              │
  ┌───────────────────────────▼────────────────────────────┐
  │ 5. PERFORMANCE OPTIMIZATION                            │
  │    - Independent subtask parallel fan-out (max 3)      │
  │    - Amdahl-calibrated speedup evaluation              │
  └────────────────────────────────────────────────────────┘
```

---

## 3. Shadow Diff Schema & Predicted Benefits

When operating in `MODE_SHADOW`, `evaluate_policy()` intercepts the candidate decision via `_apply_shadow_mode_guard()` and attaches a `shadow_diff` block:

```json
{
  "divergence": true,
  "recommended_rule_id": "INDEPENDENT_SUBTASKS_PARALLEL_APPROVED",
  "recommended_strategy": "S2",
  "recommended_workers": 2,
  "recommended_model": "GPT6_LUNA_HIGH",
  "recommended_deterministic": false,
  "executed_strategy": "S1",
  "executed_workers": 1,
  "executed_model": "GPT6_LUNA_HIGH",
  "predicted_benefits": {
    "predicted_speedup": 1.6,
    "predicted_cost_ratio": 1.0,
    "predicted_quality_delta": "Equivalent (isolated worktrees verified before merge)",
    "risk_mitigation": "Bounded to 2 concurrent workers with clean scope separation."
  },
  "justification": "Independent subtasks (2) approved for parallel execution with isolated worktrees and ALL_REQUIRED merge."
}
```

### 3.1 Divergence & Benefit Modeling
- **Deterministic Trivial Bypass:** `predicted_speedup: 15.0x`, `predicted_cost_ratio: 0.0`, zero token consumption.
- **Independent Parallel Execution:** Amdahl-calibrated speedup with worktree initialization and merge overhead ($1.45\times$ for 2 workers, $1.85\times$ for 3 workers).
- **Critical Independent Review:** `predicted_quality_delta: "+35% defect catch rate via Opus independent reviewer"`, with mandatory independent review gate.
- **Scope Collision Prevention:** `predicted_quality_delta: "+100% merge integrity (zero race condition risk)"`.

---

## 4. Health Monitoring, Fallback & Rollback Mechanisms

In addition to Shadow Mode, the engine incorporates active safety tripwires implemented in `PolicyHealthState`:

| Trigger Condition | Rule ID | Action Taken |
|---|---|---|
| **Merge Failure** (`merge_failures > 0`) | `ROLLBACK_MERGE_FAILURE` | Immediate rollback to S1, single worker, high effort, strict verification. |
| **Error Spike** (`consecutive_errors >= 2`) | `ROLLBACK_CONSECUTIVE_ERRORS` | Immediate rollback to S1, single worker, elevated review gating. |
| **High Rejections** (Mean repairs $\ge 2.0$) | `ROLLBACK_HIGH_REJECTIONS` | Immediate rollback to S1, conservative serialization. |
| **Budget Limit Warning** ($\ge 85\%$ spend) | `BUDGET_EXCEEDED_CLAMP` | Clamps model tier to economic alternatives without violating the Quality Floor. |

---

## 5. Test Verification Status

The adaptive policy engine and its shadow gating invariants have been verified across the test suite:

- `test_default_mode_is_strictly_shadow`: **PASS** (confirms default invocation produces `MODE_SHADOW`).
- `test_shadow_mode_computes_predicted_benefits`: **PASS** (confirms `shadow_diff` contains speedup, cost ratio, quality delta, and risk mitigation).
- `test_controlled_adaptive_requires_explicit_opt_in`: **PASS** (confirms non-shadow execution requires explicit configuration).
- `test_constraint_hierarchy_order`: **PASS** (confirms Safety $\to$ Validity $\to$ Quality $\to$ Budget $\to$ Performance).
- `test_rollback_on_merge_failure`: **PASS**.
- `test_rollback_on_consecutive_errors`: **PASS**.
- `test_rollback_on_high_repair_rejections`: **PASS**.
- `test_budget_clamp_enforced_without_breaking_quality_floor`: **PASS**.
- `test_critical_security_mandatory_independent_review`: **PASS**.
- `test_safety_scope_conflict_forces_serial_fallback`: **PASS**.

**Test Result:** **14 / 14 passed in 0.27s**.

---

## 6. Promotion Roadmap to Controlled Adaptive Mode

Promotion from `MODE_SHADOW` to `MODE_CONTROLLED_ADAPTIVE` will only be authorized after:
1. Minimum 100 consecutive production runs monitored in `MODE_SHADOW` with zero unexplained divergences.
2. Verified shadow speedup predictions aligning with observed wall-clock bounds.
3. Formal operator review and sign-off via Human Gate.
