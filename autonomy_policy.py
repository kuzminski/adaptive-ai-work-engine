"""Evidence-driven default policy for AAW Autonomous Iterations V0.3.

This module selects profile IDs only. Runtime model IDs and effort support stay
in IMPLEMENTER_PROFILES and MODEL_CATALOG, and the role adapter fails closed
when the selected profile is unavailable.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

POLICY_VERSION = "AAW_DEFAULT_AUTONOMOUS_POLICY_V0.3"
PRESET_ID = "DEFAULT_AUTONOMOUS"

PROFILE_KEYS = (
    "initial_planner", "implementer_default", "implementer_harder", "implementer_hard",
    "implementer_capability_escalation", "review_pretreatment", "primary_reviewer",
    "repair_default", "repair_hard", "final_review_default", "final_review_hard",
    "final_review_critical",
)


def validate_policy_ids(values: Any) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError("AUTONOMY_ROLES.policy_profiles must be an object")
    missing = [key for key in PROFILE_KEYS if not isinstance(values.get(key), str) or not values[key].strip()]
    if missing:
        raise ValueError(f"AUTONOMY_ROLES.policy_profiles missing profile IDs: {missing}")
    return {key: str(values[key]) for key in PROFILE_KEYS}


# ── implementer chain (user- or system-defined escalation order) ─────────────
#
# A chain is an ordered list of profile IDs, weakest step first. With a chain the
# implementation family (implement / repair / review-prep) climbs it in order: the
# plan's complexity picks the starting step and a blocking capability failure
# moves one step up. A single-element chain pins one model for everything. The
# legacy `implementer_*` slots stay populated from the chain (clamped to its
# length) so the human override, the UI and older records keep their meaning.

CHAIN_KEY = "implementer_chain"
MAX_CHAIN_LENGTH = 12
_COMPLEXITY_START = {"NORMAL": 0, "HARDER": 1, "SIGNIFICANTLY_DIFFICULT": 2}
# chain index each legacy slot reads from (clamped to the chain length)
CHAIN_SLOT_INDEX = {"implementer_default": 0, "implementer_harder": 1, "implementer_hard": 2,
                    "implementer_capability_escalation": 3, "repair_default": 1, "repair_hard": 2,
                    "review_pretreatment": 1}
# first chain index that counts as "escalated beyond the Luna-class tiers" for final-review severity
CHAIN_ESCALATED_FROM_INDEX = 3


def validate_chain(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("implementer_chain must be a non-empty list of profile IDs")
    chain = [str(v).strip() for v in values if isinstance(v, str) and v.strip()]
    if len(chain) != len(values):
        raise ValueError("implementer_chain entries must be non-empty profile ID strings")
    if len(set(chain)) != len(chain):
        raise ValueError("implementer_chain must not repeat a profile")
    if len(chain) > MAX_CHAIN_LENGTH:
        raise ValueError(f"implementer_chain may have at most {MAX_CHAIN_LENGTH} steps")
    return chain


def chain_slot_ids(chain: Sequence[str]) -> dict[str, str]:
    """The legacy implementation slots implied by a chain (indexes clamped to its length)."""
    chain = validate_chain(chain)
    return {slot: chain[min(index, len(chain) - 1)] for slot, index in CHAIN_SLOT_INDEX.items()}


def _chain_selection(chain: Sequence[str], index: int, reason: str, *, tier: str,
                     evidence: Sequence[Any] = (), previous_attempt: Mapping[str, Any] | None = None,
                     escalated_from: str | None = None) -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "profile_key": f"{CHAIN_KEY}[{index}]",
        "profile_id": chain[index],
        "selection_reason": reason,
        "tier": tier,
        "complexity_risk_evidence": [str(item) for item in evidence],
        "previous_attempt": dict(previous_attempt) if previous_attempt else None,
        "escalated_from": escalated_from,
        "chain_index": index,
        "chain_length": len(chain),
    }


def _chain_capability_failure(chain: Sequence[str], previous_attempt: Mapping[str, Any] | None) -> int | None:
    """Index of the chain step that failed with a concrete capability mismatch (None if none or last step)."""
    if not previous_attempt or previous_attempt.get("profile_id") not in chain:
        return None
    qualifying = (previous_attempt.get("outcome") == "FAILED"
                  and previous_attempt.get("finding_code") == "IMPLEMENTATION_CAPABILITY_MISMATCH"
                  and bool(previous_attempt.get("execution_id")) and bool(previous_attempt.get("evidence_ref")))
    index = list(chain).index(previous_attempt["profile_id"])
    return index if qualifying and index < len(chain) - 1 else None


def _selection(profile_key: str, profiles: Mapping[str, str], reason: str, *, tier: str,
               evidence: Sequence[Any] = (), previous_attempt: Mapping[str, Any] | None = None,
               escalated_from: str | None = None) -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "profile_key": profile_key,
        "profile_id": profiles[profile_key],
        "selection_reason": reason,
        "tier": tier,
        "complexity_risk_evidence": [str(item) for item in evidence],
        "previous_attempt": dict(previous_attempt) if previous_attempt else None,
        "escalated_from": escalated_from,
    }


def select_initial_planner(profiles: Mapping[str, str]) -> dict[str, Any]:
    return _selection("initial_planner", profiles, "INITIAL_ARCHITECT", tier="INITIAL")


def select_implementation(profiles: Mapping[str, str], complexity: str = "NORMAL", *,
                          evidence: Sequence[Any] = (), previous_attempt: Mapping[str, Any] | None = None,
                          human_override: Mapping[str, Any] | None = None,
                          chain: Sequence[str] | None = None) -> dict[str, Any]:
    """Choose Luna by bounded complexity; Sonnet needs concrete evidence or a human override.

    With a `chain` the starting step follows the plan's complexity and a concrete
    capability failure of a step moves to the next one; legacy tiers are not used.
    """
    value = str(complexity or "NORMAL").upper()
    route = {"NORMAL": ("implementer_default", "DEFAULT_IMPLEMENTATION"),
             "HARDER": ("implementer_harder", "IMPLEMENTATION_COMPLEXITY_ESCALATION"),
             "SIGNIFICANTLY_DIFFICULT": ("implementer_hard", "IMPLEMENTATION_COMPLEXITY_ESCALATION")}
    if value not in route:
        raise ValueError(f"unknown implementation complexity {complexity!r}")
    if human_override:
        reason = str(human_override.get("reason") or "").strip()
        if human_override.get("profile_key") != "implementer_capability_escalation" or not reason:
            raise ValueError("human implementation override must explicitly name the Sonnet escalation profile and reason")
        return _selection("implementer_capability_escalation", profiles, "HUMAN_OVERRIDE", tier="SONNET",
                          evidence=[*evidence, reason], previous_attempt=previous_attempt,
                          escalated_from=profiles["implementer_hard"])
    if chain:
        failed = _chain_capability_failure(chain, previous_attempt)
        if failed is not None:
            return _chain_selection(chain, failed + 1, "CHAIN_CAPABILITY_FAILURE", tier=f"CHAIN_STEP_{failed + 2}",
                                    evidence=[*evidence, str(previous_attempt["evidence_ref"])],
                                    previous_attempt=previous_attempt, escalated_from=chain[failed])
        key, reason = route[value]
        index = min(_COMPLEXITY_START[value], len(chain) - 1)
        return _chain_selection(chain, index, reason, tier=value, evidence=evidence)
    if previous_attempt:
        qualifying = (
            previous_attempt.get("profile_id") == profiles["implementer_hard"]
            and previous_attempt.get("outcome") == "FAILED"
            and previous_attempt.get("finding_code") == "IMPLEMENTATION_CAPABILITY_MISMATCH"
            and bool(previous_attempt.get("execution_id"))
            and bool(previous_attempt.get("evidence_ref"))
        )
        if qualifying:
            return _selection("implementer_capability_escalation", profiles,
                              "LUNA_MAX_CAPABILITY_FAILURE", tier="SONNET",
                              evidence=[*evidence, str(previous_attempt["evidence_ref"])],
                              previous_attempt=previous_attempt,
                              escalated_from=profiles["implementer_hard"])
    key, reason = route[value]
    return _selection(key, profiles, reason, tier=value, evidence=evidence)


def select_repair(profiles: Mapping[str, str], *, attempt: int,
                  findings: Sequence[Mapping[str, Any]],
                  previous_attempt: Mapping[str, Any] | None = None,
                  chain: Sequence[str] | None = None) -> dict[str, Any]:
    if chain:
        capability_finding = next((f for f in findings
                                   if f.get("finding_code") == "IMPLEMENTATION_CAPABILITY_MISMATCH"
                                   and f.get("blocking") is True and f.get("evidence_ref")), None)
        failed = _chain_capability_failure(chain, previous_attempt) if capability_finding else None
        if failed is not None:
            return _chain_selection(chain, failed + 1, "CHAIN_CAPABILITY_FAILURE", tier="CHAIN_REPAIR",
                                    evidence=[str(capability_finding["evidence_ref"])],
                                    previous_attempt=previous_attempt, escalated_from=chain[failed])
    elif previous_attempt and previous_attempt.get("profile_id") == profiles["implementer_hard"]:
        capability_finding = next((f for f in findings
                                   if f.get("finding_code") == "IMPLEMENTATION_CAPABILITY_MISMATCH"
                                   and f.get("blocking") is True), None)
        if (capability_finding and previous_attempt.get("outcome") == "FAILED"
                and previous_attempt.get("execution_id") and capability_finding.get("evidence_ref")):
            return _selection("implementer_capability_escalation", profiles,
                              "LUNA_MAX_CAPABILITY_FAILURE", tier="SONNET_REPAIR",
                              evidence=[str(capability_finding["evidence_ref"])],
                              previous_attempt=previous_attempt,
                              escalated_from=profiles["implementer_hard"])
    hard = attempt > 1 or len([f for f in findings if f.get("blocking")]) > 1 or any(
        str(f.get("severity", "")).upper() == "CRITICAL" for f in findings)
    key = "repair_hard" if hard else "repair_default"
    reason = "REPAIR_COMPLEXITY_ESCALATION" if hard else "DEFAULT_REPAIR"
    return _selection(key, profiles, reason, tier="HARD" if hard else "DEFAULT",
                      evidence=[str(f.get("evidence_ref") or f.get("finding_key") or "finding") for f in findings])


_CRITICAL_PATHS = {
    "execution_contract.py", "execution_ledger.py", "autonomy_contract.py",
    "autonomy_run_lock.py", "run_recovery.py", "run_cancellation.py", "workflow_runner.py",
    "process_observation.py", "autonomy_adapters.py",
}
_ARCHITECTURE_PATHS = {"autonomy_controller.py"}


def select_final_review(profiles: Mapping[str, str], *, changed_files: Sequence[str],
                        repair_attempts: int, findings: Sequence[Mapping[str, Any]],
                        human_critical: bool = False, implementation_profile_id: str | None = None,
                        uncertainty: bool = False, chain: Sequence[str] | None = None) -> dict[str, Any]:
    files = {str(path).replace("\\", "/").rsplit("/", 1)[-1].lower() for path in changed_files}
    severe = [f for f in findings if str(f.get("severity", "")).upper() in {"HIGH", "CRITICAL"}]
    critical_evidence = []
    if human_critical:
        critical_evidence.append("HUMAN_DEFINED_CRITICAL_SCOPE")
    critical_evidence.extend(f"CRITICAL_CONTRACT_CHANGE:{name}" for name in sorted(files & _CRITICAL_PATHS))
    critical_evidence.extend(f"CRITICAL_FINDING:{f.get('finding_key') or f.get('summary', 'finding')}"
                             for f in findings if str(f.get("severity", "")).upper() == "CRITICAL")
    if critical_evidence:
        return _selection("final_review_critical", profiles, "CRITICAL_CONTRACT_CHANGE" if files & _CRITICAL_PATHS
                          else "HUMAN_DEFINED_CRITICAL_SCOPE" if human_critical else "FINAL_REVIEW_CRITICAL_FINDING",
                          tier="CRITICAL", evidence=critical_evidence,
                          escalated_from=profiles["final_review_hard"])
    hard_evidence = []
    hard_evidence.extend(f"ARCHITECTURE_CHANGE:{name}" for name in sorted(files & _ARCHITECTURE_PATHS))
    if repair_attempts >= 2:
        hard_evidence.append(f"REPEATED_REPAIR:{repair_attempts}")
    if len(severe) >= 2:
        hard_evidence.append(f"MULTIPLE_SEVERE_FINDINGS:{len(severe)}")
    if chain:
        if implementation_profile_id in chain and list(chain).index(implementation_profile_id) >= CHAIN_ESCALATED_FROM_INDEX:
            hard_evidence.append("IMPLEMENTATION_ESCALATED_BEYOND_LUNA_MAX")
    elif implementation_profile_id == profiles["implementer_capability_escalation"]:
        hard_evidence.append("IMPLEMENTATION_ESCALATED_BEYOND_LUNA_MAX")
    if uncertainty:
        hard_evidence.append("REVIEW_UNCERTAINTY")
    if hard_evidence:
        return _selection("final_review_hard", profiles, "FINAL_REVIEW_ARCHITECTURE_RISK"
                          if files & _ARCHITECTURE_PATHS else "FINAL_REVIEW_REPEATED_REPAIR"
                          if repair_attempts >= 2 else "FINAL_REVIEW_HIGH_IMPACT_RISK",
                          tier="HARD", evidence=hard_evidence,
                          escalated_from=profiles["final_review_default"])
    return _selection("final_review_default", profiles, "DEFAULT_FINAL_REVIEW", tier="DEFAULT")


def select_continuation_planner(profiles: Mapping[str, str], final_selection: Mapping[str, Any]) -> dict[str, Any]:
    tier = str(final_selection.get("tier", "DEFAULT")).upper()
    key = {"DEFAULT": "final_review_default", "HARD": "final_review_hard",
           "CRITICAL": "final_review_critical"}.get(tier)
    if key is None:
        raise ValueError(f"unknown final-review tier {tier!r}")
    return _selection(key, profiles, "CONTINUATION_FROM_FINAL_REVIEW_TIER", tier=tier,
                      evidence=final_selection.get("complexity_risk_evidence", []),
                      previous_attempt={"final_review_profile_id": final_selection.get("profile_id"),
                                        "iteration_id": final_selection.get("iteration_id")})


def profile_id_map(policy_bindings: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    return {key: str(binding["profile_id"]) for key, binding in policy_bindings.items()
            if isinstance(binding, Mapping) and isinstance(binding.get("profile_id"), str)}


# ── V0.4: bounded repair escalation (roles, not model names) ─────────────────

def default_repair_escalation(profiles: Mapping[str, str], chain: Sequence[str] | None = None) -> dict[str, Any]:
    """The ladder implied by the policy profiles when AUTONOMY_ROLES does not configure one.

    Roles only: `default_implementer` is the implementer family's profile,
    `difficult_implementer` the capability-escalation profile, `planner` the
    initial planner. Changing a profile ID in `policy_profiles` (or supplying
    an explicit `repair_escalation` block) changes the ladder; this module
    names no model.
    """
    if chain:
        # The repair ladder walks the implementer chain in order; the last step is the "difficult implementer".
        chain = validate_chain(chain)
        return {"enabled": True, "stages": ["CURRENT", "EFFORT_UP", "DIFFICULT_IMPLEMENTER", "PLANNER_DIAGNOSIS"],
                "max_effort_steps": min(6, max(1, len(chain) - 3)), "max_attempts_per_stage": 2,
                "effort_ladder": list(chain),
                "roles": {"default_implementer": chain[0], "difficult_implementer": chain[-1],
                          "planner": profiles["initial_planner"], "reviewer": profiles["primary_reviewer"],
                          "final_reviewer": profiles["final_review_default"]}}
    return {"enabled": True, "stages": ["CURRENT", "EFFORT_UP", "DIFFICULT_IMPLEMENTER", "PLANNER_DIAGNOSIS"],
            "max_effort_steps": 1, "max_attempts_per_stage": 2,
            "effort_ladder": [profiles["implementer_default"], profiles["implementer_harder"],
                              profiles["implementer_hard"]],
            "roles": {"default_implementer": profiles["implementer_default"],
                      "difficult_implementer": profiles["implementer_capability_escalation"],
                      "planner": profiles["initial_planner"], "reviewer": profiles["primary_reviewer"],
                      "final_reviewer": profiles["final_review_default"]}}


def select_repair_step(step: Mapping[str, Any], *, profile_id: str, previous_profile_id: str | None,
                       evidence: Sequence[Any] = ()) -> dict[str, Any]:
    """A selection record for an escalated repair (or diagnosis) step; same shape as `_selection`."""
    return {"policy_version": POLICY_VERSION, "profile_key": "repair_escalation_" + str(step["stage"]).lower(),
            "profile_id": profile_id, "selection_reason": "REPAIR_ESCALATION:" + str(step["stage"]),
            "tier": str(step["stage"]), "complexity_risk_evidence": [str(e) for e in evidence],
            "previous_attempt": None, "escalated_from": previous_profile_id}
