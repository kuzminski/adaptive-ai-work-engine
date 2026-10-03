"""Quota / trust / capability routing: pure decision tests for `model_router`."""

import datetime as dt

import pytest

import model_router as mr

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
CAPS = ["code_edit", "shell", "structured_output", "review", "planning"]


def cand(pid, pool, *, cls=4, trust="PRODUCTION", caps=CAPS, available=True, model=None, **extra):
    return {"profile_id": pid, "pool": pool, "runtime_model_id": model or pid.lower(), "trust": trust,
            "capability_class": cls, "capabilities": caps, "available": available, **extra}


def limit(percent, certainty="EXACT", window="5h", resets_in=None, **extra):
    row = {"window": window, "certainty": certainty, "remaining_percent": percent}
    if resets_in is not None:
        row["resets_in_minutes"] = resets_in
    if certainty == "ESTIMATED":
        row.setdefault("provenance", "usage-counter")
    return {**row, **extra}


def tel(**pools):
    return {"pools": {name: {"limits": limits if isinstance(limits, list) else [limits]}
                      for name, limits in pools.items()}}


def req(**over):
    base = {"phase": "execute", "task_class": "MEDIUM", "required_capabilities": ["code_edit"],
            "preferred_profile_id": "A"}
    return {**base, **over}


CANDS = [cand("A", "pa"), cand("B", "pb"), cand("C", "pc", cls=2)]


def decide(request=None, cands=CANDS, telemetry=None, state=None, policy=None, now=NOW):
    return mr.route(request or req(), cands, telemetry, state, policy=policy, now=now)


# ── defaults ─────────────────────────────────────────────────────────────────

def test_default_policy_values_are_the_specified_ones():
    p = mr.normalize_policy()
    assert (p["soft_threshold_percent"], p["reserve_percent"], p["near_reset_minutes"],
            p["return_threshold_percent"]) == (10, 5, 20, 25)


def test_policy_rejects_inconsistent_thresholds():
    with pytest.raises(mr.RouterError):
        mr.normalize_policy({"reserve_percent": 12})
    with pytest.raises(mr.RouterError):
        mr.normalize_policy({"return_threshold_percent": 5})


# ── 1. normal quota ──────────────────────────────────────────────────────────

def test_normal_quota_keeps_the_preferred_profile():
    d = decide(telemetry=tel(pa=limit(60), pb=limit(95)))
    assert d["selected_profile_id"] == "A" and d["reason_code"] == "QUOTA_OK" and not d["switched"]
    assert d["selected_quota"]["certainty"] == "EXACT" and d["selected_trust"] == "PRODUCTION"


def test_exactly_ten_percent_is_still_normal_work():
    assert decide(telemetry=tel(pa=limit(10), pb=limit(90)))["reason_code"] == "QUOTA_OK"


# ── 2. <10% + better alternative ─────────────────────────────────────────────

def test_below_soft_threshold_prefers_an_adequate_alternative_with_more_headroom():
    d = decide(telemetry=tel(pa=limit(9), pb=limit(80)))
    assert d["selected_profile_id"] == "B" and d["reason_code"] == "QUOTA_LOW_ALTERNATIVE" and d["switched"]
    assert "pa" in d["router_state"]["demoted"] and d["switch"]["from_profile_id"] == "A"
    assert d["ranked_profile_ids"][0] == "B" and "A" in d["ranked_profile_ids"]


def test_low_quota_without_a_better_alternative_stays():
    d = decide(telemetry=tel(pa=limit(9), pb=limit(7)))
    assert d["selected_profile_id"] == "A" and d["reason_code"] == "QUOTA_LOW_NO_BETTER_ALTERNATIVE"


def test_alternative_must_be_adequate_not_just_roomy():
    # C has lots of quota but a lower capability class than the MEDIUM floor... class 2 == floor, so use VERY_LARGE.
    d = decide(req(task_class="VERY_LARGE"), telemetry=tel(pa=limit(8), pb=limit(5), pc=limit(99)))
    assert d["selected_profile_id"] == "A"
    assert any(r["profile_id"] == "C" and r["rejected_by"][0].startswith("CLASS_BELOW_FLOOR")
               for r in d["alternatives"])


# ── 3. near reset ────────────────────────────────────────────────────────────

def test_near_reset_lets_a_small_task_stay_on_the_current_provider():
    d = decide(req(task_class="SMALL", current_pool="pa"), telemetry=tel(pa=limit(7, resets_in=12), pb=limit(90)))
    assert d["selected_profile_id"] == "A" and d["reason_code"] == "NEAR_RESET_STAY"


def test_near_reset_does_not_apply_to_larger_tasks_or_a_late_reset():
    big = decide(req(task_class="LARGE", current_pool="pa"), telemetry=tel(pa=limit(7, resets_in=12), pb=limit(90)))
    late = decide(req(task_class="SMALL", current_pool="pa"), telemetry=tel(pa=limit(7, resets_in=45), pb=limit(90)))
    assert big["selected_profile_id"] == "B" and late["selected_profile_id"] == "B"


# ── 4. reserve ───────────────────────────────────────────────────────────────

def test_reserve_is_not_used_by_ordinary_execute():
    d = decide(telemetry=tel(pa=limit(4), pb=limit(70)))
    assert d["selected_profile_id"] == "B" and d["reason_code"] == "RESERVE_PROTECTED_SWITCH"


def test_reserve_is_kept_for_review_repair_and_final_review():
    for phase in ("review", "repair", "final_review"):
        d = decide(req(phase=phase, required_capabilities=["review"]), telemetry=tel(pa=limit(4), pb=limit(70)))
        assert d["selected_profile_id"] == "A" and d["reason_code"] == "RESERVE_PROTECTED_PHASE_ALLOWED", phase


def test_only_reserve_left_means_no_eligible_profile_unless_last_resort_is_enabled():
    t = tel(pa=limit(4), pb=limit(3), pc=limit(3))
    assert decide(telemetry=t)["selected_profile_id"] is None
    last = decide(telemetry=t, policy={"reserve_last_resort": True})
    assert last["selected_profile_id"] == "A" and last["reason_code"] == "RESERVE_LAST_RESORT"


# ── 5. hysteresis 10% / 25% ──────────────────────────────────────────────────

def test_hysteresis_holds_between_ten_and_twenty_five_percent():
    first = decide(telemetry=tel(pa=limit(8), pb=limit(80)))
    assert first["selected_profile_id"] == "B"
    # A recovers to 15% — above soft (10) but below the return threshold (25): stay on B.
    held = decide(req(current_pool="pb"), telemetry=tel(pa=limit(15), pb=limit(78)), state=first["router_state"])
    assert held["selected_profile_id"] == "B" and held["reason_code"] == "HYSTERESIS_HOLD"
    # A reaches 25%: return.
    back = decide(req(current_pool="pb"), telemetry=tel(pa=limit(25), pb=limit(78)), state=held["router_state"])
    assert back["selected_profile_id"] == "A" and back["reason_code"].startswith("RETURN_")
    assert "pa" not in back["router_state"]["demoted"]


def test_confirmed_reset_returns_the_provider_before_the_threshold():
    first = decide(telemetry=tel(pa=limit(8), pb=limit(80)))
    since = first["router_state"]["demoted"]["pa"]["since"]
    reset_at = (dt.datetime.fromisoformat(since) + dt.timedelta(minutes=30)).isoformat()
    t = tel(pa=[limit(15, last_reset_at=reset_at)], pb=limit(78))
    later = decide(req(current_pool="pb"), telemetry=t, state=first["router_state"], now=NOW + dt.timedelta(minutes=40))
    assert later["selected_profile_id"] == "A" and later["reason_code"] == "RETURN_RESET_CONFIRMED"


def test_returns_when_no_better_alternative_exists():
    first = decide(telemetry=tel(pa=limit(8), pb=limit(80)))
    d = decide(req(current_pool="pb"), telemetry=tel(pa=limit(15), pb=limit(6)), state=first["router_state"])
    assert d["selected_profile_id"] == "A" and d["reason_code"] == "QUOTA_LOW_NO_BETTER_ALTERNATIVE"


# ── 6. UNKNOWN telemetry ─────────────────────────────────────────────────────

def test_unknown_telemetry_changes_nothing_and_invents_no_percent():
    d = decide(telemetry=tel(pa={"window": "5h", "certainty": "UNKNOWN", "remaining_percent": 3}, pb=limit(90)))
    assert d["selected_profile_id"] == "A" and d["reason_code"] == "QUOTA_UNKNOWN_NO_CHANGE"
    assert d["selected_quota"]["percent"] is None and d["selected_quota"]["certainty"] == "UNKNOWN"
    assert any("discarded" in n for n in d["evaluated"][0]["limits"][0]["notes"])


def test_no_telemetry_at_all_is_unknown():
    d = decide()
    assert d["selected_profile_id"] == "A" and d["selected_quota"]["certainty"] == "UNKNOWN"


def test_estimated_requires_provenance_and_stale_exact_is_demoted():
    bare = mr.normalize_limit({"certainty": "ESTIMATED", "remaining_percent": 4}, now=NOW,
                              policy=mr.normalize_policy())
    assert bare["certainty"] == "UNKNOWN" and bare["remaining_percent"] is None
    stale = mr.normalize_limit({"certainty": "EXACT", "remaining_percent": 40,
                                "observed_at": (NOW - dt.timedelta(minutes=45)).isoformat()},
                               now=NOW, policy=mr.normalize_policy())
    assert stale["certainty"] == "ESTIMATED" and "stale" in stale["provenance"]
    old = mr.normalize_limit({"certainty": "EXACT", "remaining_percent": 40,
                              "observed_at": (NOW - dt.timedelta(hours=5)).isoformat()},
                             now=NOW, policy=mr.normalize_policy())
    assert old["certainty"] == "UNKNOWN" and old["remaining_percent"] is None


# ── 7. several simultaneous limits ───────────────────────────────────────────

def test_the_most_restrictive_active_limit_decides():
    limits = [limit(80, window="5h"), limit(45, window="daily"), limit(6, window="weekly", resets_in=3000),
              limit(None, "UNKNOWN", window="cost")]
    d = decide(telemetry=tel(pa=limits, pb=limit(70)))
    assert d["selected_profile_id"] == "B"
    view = next(r for r in d["evaluated"] if r["profile_id"] == "A")["quota"]
    assert view["binding_limit"] == "weekly" and view["percent"] == 6 and view["unknown_windows"] == ["cost"]


def test_a_model_scoped_limit_only_applies_to_that_model():
    limits = [limit(90), limit(2, window="model", model="other-model")]
    assert decide(telemetry=tel(pa=limits, pb=limit(50)))["selected_profile_id"] == "A"
    limits = [limit(90), limit(2, window="model", model="a")]
    assert decide(telemetry=tel(pa=limits, pb=limit(50)))["selected_profile_id"] == "B"


def test_a_blocking_rate_limit_removes_the_provider_until_it_lifts():
    d = decide(telemetry=tel(pa=[limit(90), {"window": "rate", "kind": "RATE", "blocked": True,
                                             "certainty": "EXACT", "retry_after_minutes": 7}], pb=limit(50)))
    assert d["selected_profile_id"] == "B" and d["reason_code"] == "PREFERRED_INELIGIBLE:QUOTA_EXHAUSTED"


def test_exact_zero_is_exhausted_but_estimated_zero_is_only_reserve():
    assert decide(telemetry=tel(pa=limit(0), pb=limit(50)))["reason_code"].startswith("PREFERRED_INELIGIBLE")
    assert decide(telemetry=tel(pa=limit(0, "ESTIMATED"), pb=limit(50)))["reason_code"] == "RESERVE_PROTECTED_SWITCH"


# ── 8. capability mismatch ───────────────────────────────────────────────────

def test_capability_mismatch_excludes_a_profile_regardless_of_quota():
    cands = [cand("A", "pa", caps=["review"]), cand("B", "pb"), cand("D", "pd", caps=["review"])]
    d = decide(req(required_capabilities=["code_edit", "shell"]), cands, tel(pa=limit(99), pb=limit(15), pd=limit(99)))
    assert d["selected_profile_id"] == "B"
    assert any("CAPABILITY_MISMATCH" in r["rejected_by"][0] for r in d["alternatives"] if r["profile_id"] == "D")


def test_strong_model_is_not_displaced_by_a_weaker_one_with_lots_of_quota():
    weak = [cand("A", "pa", cls=4), cand("W", "pw", cls=1)]
    d = decide(req(task_class="LARGE"), weak, tel(pa=limit(8), pw=limit(100)))
    assert d["selected_profile_id"] == "A"


# ── 9. experimental provider ─────────────────────────────────────────────────

AG = cand("AG", "pg", cls=2, trust="EXPERIMENTAL", caps=["code_edit", "shell", "structured_output"])


def test_experimental_provider_may_take_small_and_medium_work():
    cands = [cand("A", "pa"), AG]
    for size in ("SMALL", "MEDIUM"):
        d = decide(req(task_class=size), cands, tel(pa=limit(6), pg=limit(90)))
        assert d["selected_profile_id"] == "AG", size
        assert d["selected_trust"] == "EXPERIMENTAL"


def test_experimental_provider_never_takes_critical_or_very_large_or_review_work():
    cands = [cand("A", "pa", cls=4), AG]
    t = tel(pa=limit(6), pg=limit(90))
    assert decide(req(task_class="VERY_LARGE"), cands, t)["selected_profile_id"] == "A"
    assert decide(req(criticality="CRITICAL"), cands, t)["selected_profile_id"] == "A"
    review = decide(req(phase="final_review", required_capabilities=[]), cands, t)
    assert review["selected_profile_id"] == "A"
    assert "EXPERIMENTAL_PHASE_NOT_ALLOWED" in next(r for r in review["alternatives"] if r["profile_id"] == "AG")["rejected_by"]


def test_disabled_trust_is_never_selected():
    d = decide(req(preferred_profile_id="A"), [cand("A", "pa", trust="DISABLED"), cand("B", "pb")], None)
    assert d["selected_profile_id"] == "B" and d["reason_code"].startswith("PREFERRED_INELIGIBLE:TRUST_DISABLED")


# ── 10. switch penalty ───────────────────────────────────────────────────────

def test_switch_penalty_prefers_the_provider_already_in_use_between_equal_alternatives():
    cands = [cand("A", "pa"), cand("B", "pb"), cand("B2", "pc")]
    t = tel(pa=limit(5), pb=limit(70), pc=limit(70))
    from_b = decide(req(current_pool="pb"), cands, t)
    from_c = decide(req(current_pool="pc"), cands, t)
    assert from_b["selected_profile_id"] == "B" and from_c["selected_profile_id"] == "B2"
    pen = next(r for r in from_b["evaluated"] if r["profile_id"] == "B2")["score"]["components"]["switch_penalty"]
    assert pen < 0 and from_b["switch_penalty_applied"] == 0.0


def test_switch_penalty_grows_with_task_size():
    cands = [cand("A", "pa"), cand("B", "pb")]
    t = tel(pa=limit(5), pb=limit(70))
    small = decide(req(task_class="SMALL", current_pool="pa"), cands, t)["switch_penalty_applied"]
    huge = decide(req(task_class="VERY_LARGE", current_pool="pa"), cands, t)["switch_penalty_applied"]
    assert huge < small < 0


# ── 11. manual override ──────────────────────────────────────────────────────

def test_manual_override_beats_quota_and_trust_heuristics():
    cands = [cand("A", "pa"), cand("B", "pb"), AG]
    d = decide(req(manual_override="AG", task_class="VERY_LARGE"), cands, tel(pa=limit(90), pg=limit(1)))
    assert d["selected_profile_id"] == "AG" and d["reason_code"] == "MANUAL_OVERRIDE"


def test_infeasible_manual_override_falls_back_to_the_heuristics_and_says_so():
    cands = [cand("A", "pa"), cand("B", "pb", available=False)]
    d = decide(req(manual_override="B"), cands, None)
    assert d["selected_profile_id"] == "A" and "MANUAL_OVERRIDE_INFEASIBLE:NOT_AVAILABLE" in d["detail"]
    d = decide(req(manual_override="ghost"), cands, None)
    assert "MANUAL_OVERRIDE_INFEASIBLE:UNKNOWN_PROFILE" in d["detail"]


# ── 12. timeout / rate-limit / auth ──────────────────────────────────────────

@pytest.mark.parametrize("failure,minutes", [("TIMEOUT", 10), ("RATE_LIMIT", 15), ("AUTH", 60)])
def test_a_hard_provider_failure_routes_around_that_provider_until_it_cools_down(failure, minutes):
    state = mr.record_failure(None, "pa", failure, now=NOW)
    d = decide(state=state)
    assert d["selected_profile_id"] == "B" and d["reason_code"] == "PREFERRED_INELIGIBLE:PROVIDER_BLOCKED"
    later = decide(state=state, now=NOW + dt.timedelta(minutes=minutes + 1))
    assert later["selected_profile_id"] == "A"
    assert mr.record_success(state, "pa")["health"] == {}


def test_rate_limit_honours_retry_after():
    state = mr.record_failure(None, "pa", "RATE_LIMIT", now=NOW, retry_after_minutes=3)
    assert decide(state=state, now=NOW + dt.timedelta(minutes=2))["selected_profile_id"] == "B"
    assert decide(state=state, now=NOW + dt.timedelta(minutes=4))["selected_profile_id"] == "A"


def test_other_failures_are_not_provider_health_signals():
    assert mr.record_failure(None, "pa", "OTHER", now=NOW)["health"] == {}


# ── 13. router disabled / guards ─────────────────────────────────────────────

def test_disabled_router_returns_the_preferred_profile_unchanged():
    d = decide(telemetry=tel(pa=limit(1), pb=limit(99)), policy={"enabled": False})
    assert d["selected_profile_id"] == "A" and d["reason_code"] == "ROUTER_DISABLED" and not d["switched"]


def test_in_progress_repo_modifying_operation_is_not_migrated_for_a_low_estimate():
    d = decide(req(operation_in_progress=True, repo_modifying=True, current_profile_id="A", current_pool="pa"),
               telemetry=tel(pa=limit(6, "ESTIMATED"), pb=limit(90)))
    assert d["selected_profile_id"] == "A" and d["reason_code"] == "OPERATION_IN_PROGRESS_NO_MIGRATE"
    hard = decide(req(operation_in_progress=True, repo_modifying=True, current_profile_id="A", current_pool="pa"),
                  telemetry=tel(pa=limit(0), pb=limit(90)))
    assert hard["selected_profile_id"] == "B"


def test_reviewer_independence_exclusion_is_honoured():
    d = decide(req(phase="review", required_capabilities=[], exclude_models=["a"]), telemetry=tel(pa=limit(90)))
    assert d["selected_profile_id"] == "B"


def test_unroutable_preferred_profile_is_left_unchanged():
    d = decide(req(preferred_profile_id="UNMAPPED"), CANDS, None)
    assert d["selected_profile_id"] == "UNMAPPED" and d["reason_code"] == "PREFERRED_NOT_ROUTABLE"


def test_decision_is_a_complete_audit_record():
    d = decide(telemetry=tel(pa=limit(9, resets_in=300), pb=limit(80, "ESTIMATED")))
    for key in ("selected_profile_id", "selected_pool", "preferred_profile_id", "alternatives", "selected_quota",
                "reason_code", "selected_trust", "task_class", "thresholds", "switch_penalty_applied", "evaluated",
                "ranked_profile_ids", "router_state", "required_capabilities"):
        assert key in d
    row = next(r for r in d["evaluated"] if r["profile_id"] == "B")
    assert row["score"]["components"].keys() >= {"trust", "zone", "quota_headroom", "switch_penalty", "capability_overshoot"}
    assert row["quota"]["certainty"] == "ESTIMATED" and row["limits"][0]["provenance"]
