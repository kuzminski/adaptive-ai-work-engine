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
    "implementer_strong", "implementer_capability_escalation", "review_pretreatment", "primary_reviewer",
    "repair_default", "repair_hard", "final_review_default", "final_review_hard",
    "final_review_critical", "continuation_planner",
)
# Slots added after runs were already frozen (V0.4 implementer-effectiveness). A frozen run
# without them keeps its old behaviour: the slot resolves to the profile it used before.
OPTIONAL_PROFILE_FALLBACKS = {
    "implementer_strong": "implementer_hard",
    "continuation_planner": "final_review_default",
}


def with_fallbacks(values: Mapping[str, str]) -> dict[str, str]:
    ids = dict(values)
    for key, fallback in OPTIONAL_PROFILE_FALLBACKS.items():
        if not (isinstance(ids.get(key), str) and ids[key].strip()) and ids.get(fallback):
            ids[key] = ids[fallback]
    return ids


def validate_policy_ids(values: Any) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError("AUTONOMY_ROLES.policy_profiles must be an object")
    values = with_fallbacks(values)
    missing = [key for key in PROFILE_KEYS if not isinstance(values.get(key), str) or not values[key].strip()]
    if missing:
        raise ValueError(f"AUTONOMY_ROLES.policy_profiles missing profile IDs: {missing}")
    return {key: str(values[key]) for key in PROFILE_KEYS}


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
                          difficulty: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Choose the implementer by visible difficulty.

    NORMAL → the default bounded implementer; HARDER → its harder tier;
    SIGNIFICANTLY_DIFFICULT, or a work packet that is too large, cross-cutting
    or under-specified for a bounded weak implementer (`work_packet.assess_difficulty`
    route STRONG) → the STRONG implementer up front, instead of a long repair
    thread by a weaker model. Sonnet capability escalation still needs concrete
    evidence or a human override.
    """
    profiles = with_fallbacks(profiles)
    value = str(complexity or "NORMAL").upper()
    route = {"NORMAL": ("implementer_default", "DEFAULT_IMPLEMENTATION"),
             "HARDER": ("implementer_harder", "IMPLEMENTATION_COMPLEXITY_ESCALATION"),
             "SIGNIFICANTLY_DIFFICULT": ("implementer_strong", "IMPLEMENTATION_COMPLEXITY_ESCALATION")}
    if value not in route:
        raise ValueError(f"unknown implementation complexity {complexity!r}")
    difficulty_route = str((difficulty or {}).get("route") or "DEFAULT").upper()
    difficulty_reasons = [f"WORK_PACKET:{r}" for r in (difficulty or {}).get("reasons", [])]
    if human_override:
        reason = str(human_override.get("reason") or "").strip()
        if human_override.get("profile_key") != "implementer_capability_escalation" or not reason:
            raise ValueError("human implementation override must explicitly name the Sonnet escalation profile and reason")
        return _selection("implementer_capability_escalation", profiles, "HUMAN_OVERRIDE", tier="SONNET",
                          evidence=[*evidence, reason], previous_attempt=previous_attempt,
                          escalated_from=profiles["implementer_hard"])
    if previous_attempt:
        qualifying = (
            previous_attempt.get("profile_id") in (profiles["implementer_hard"], profiles["implementer_strong"])
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
    if difficulty_route == "STRONG" and value != "SIGNIFICANTLY_DIFFICULT":
        return _selection("implementer_strong", profiles, "WORK_PACKET_DIFFICULTY", tier="STRONG",
                          evidence=[*evidence, *difficulty_reasons])
    if difficulty_route == "HARDER" and value == "NORMAL":
        return _selection("implementer_harder", profiles, "WORK_PACKET_DIFFICULTY", tier="HARDER",
                          evidence=[*evidence, *difficulty_reasons])
    key, reason = route[value]
    return _selection(key, profiles, reason, tier="STRONG" if key == "implementer_strong" else value,
                      evidence=evidence)


def select_repair(profiles: Mapping[str, str], *, attempt: int,
                  findings: Sequence[Mapping[str, Any]],
                  previous_attempt: Mapping[str, Any] | None = None,
                  implementation_profile_id: str | None = None) -> dict[str, Any]:
    profiles = with_fallbacks(profiles)
    if previous_attempt and previous_attempt.get("profile_id") in (profiles["implementer_hard"],
                                                                  profiles["implementer_strong"]):
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
    strong = profiles["implementer_strong"]
    if implementation_profile_id and implementation_profile_id == strong and strong not in (
            profiles["repair_default"], profiles["repair_hard"]):
        # Work the strong implementer was given because it was visibly hard is not handed to a weaker repairer.
        return _selection("implementer_strong", profiles, "REPAIR_KEEPS_STRONG_IMPLEMENTER", tier="STRONG",
                          evidence=[str(f.get("evidence_ref") or f.get("finding_key") or "finding") for f in findings])
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
                        uncertainty: bool = False) -> dict[str, Any]:
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
    if implementation_profile_id == profiles["implementer_capability_escalation"]:
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
    """Plan the next iteration. DEFAULT uses the dedicated continuation planner (a model strong
    enough to write a concrete work packet), HARD/CRITICAL the final-review tier as before."""
    profiles = with_fallbacks(profiles)
    tier = str(final_selection.get("tier", "DEFAULT")).upper()
    key = {"DEFAULT": "continuation_planner", "HARD": "final_review_hard",
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

def default_repair_escalation(profiles: Mapping[str, str]) -> dict[str, Any]:
    """The ladder implied by the policy profiles when AUTONOMY_ROLES does not configure one.

    Roles only: `default_implementer` is the implementer family's profile,
    `difficult_implementer` the capability-escalation profile, `planner` the
    initial planner. Changing a profile ID in `policy_profiles` (or supplying
    an explicit `repair_escalation` block) changes the ladder; this module
    names no model.
    """
    strong = profiles.get("implementer_strong")
    # With a STRONG implementer configured, a finding that survived the weak repairer goes straight to it
    # (fresh context, diagnosis first) instead of another, longer attempt by the same weak model family.
    return {"enabled": True, "stages": ["CURRENT", "EFFORT_UP", "DIFFICULT_IMPLEMENTER", "PLANNER_DIAGNOSIS"],
            "max_effort_steps": 0 if strong else 1, "max_attempts_per_stage": 2,
            "effort_ladder": [profiles["implementer_default"], profiles["implementer_harder"],
                              profiles["implementer_hard"]],
            "roles": {"default_implementer": profiles["implementer_default"],
                      "difficult_implementer": strong or profiles["implementer_capability_escalation"],
                      "planner": profiles["initial_planner"], "reviewer": profiles["primary_reviewer"],
                      "final_reviewer": profiles["final_review_default"]}}


def select_repair_step(step: Mapping[str, Any], *, profile_id: str, previous_profile_id: str | None,
                       evidence: Sequence[Any] = ()) -> dict[str, Any]:
    """A selection record for an escalated repair (or diagnosis) step; same shape as `_selection`."""
    return {"policy_version": POLICY_VERSION, "profile_key": "repair_escalation_" + str(step["stage"]).lower(),
            "profile_id": profile_id, "selection_reason": "REPAIR_ESCALATION:" + str(step["stage"]),
            "tier": str(step["stage"]), "complexity_risk_evidence": [str(e) for e in evidence],
            "previous_attempt": None, "escalated_from": previous_profile_id}
