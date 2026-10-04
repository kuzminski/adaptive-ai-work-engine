"""Implementer escalation chain: any model(s) chosen at start, in the user's order (or the system default).

Policy and contract units, product resolution (default chain, user chain, single model) and the real
controller driven through the chain. The scripted executors stand in for the provider boundary.
"""

import json
from pathlib import Path

import pytest

import autonomy_contract as ac
import autonomy_policy as ap
import product_recommendations as pr
import repair_escalation as rx
from test_autonomy import mandate_fixture, ok_checks
from test_product_mvp import CODEX_RUNNABLE
from test_repair_escalation import Rig, one_item
from test_autonomy_policy_v0_3 import IDS, initial_plan

ROOT = Path(__file__).parent
PROFILES = {p["profile_id"]: p for p in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
DEFAULT_CHAIN = ["GPT6_LUNA_HIGH", "GPT6_LUNA_VERY_HIGH", "GPT6_LUNA_MAX",
                 "TERRA_HIGH", "TERRA_VERY_HIGH", "TERRA_MAX",
                 "CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH"]
ALL_RUNNABLE = set(CODEX_RUNNABLE) | {"TERRA_VERY_HIGH", "TERRA_MAX",
                                      "CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH", "SONNET_HIGH"}
CAPABILITY = {"finding_key": "CAP", "severity": "HIGH", "blocking": True, "summary": "model cannot do this",
              "finding_code": "IMPLEMENTATION_CAPABILITY_MISMATCH", "evidence_ref": "EXECUTION_RESULT:EXE_1"}


def failed(chain, index):
    return {"profile_id": chain[index], "outcome": "FAILED", "execution_id": "EXE_1",
            "finding_code": "IMPLEMENTATION_CAPABILITY_MISMATCH", "evidence_ref": "EXECUTION_RESULT:EXE_1"}


# ── catalog ──────────────────────────────────────────────────────────────────

def test_new_chain_profiles_are_exact_models_with_the_requested_efforts():
    assert (PROFILES["TERRA_HIGH"]["runtime_model_id"], PROFILES["TERRA_HIGH"]["effort"]) == ("gpt-5.6-terra", "high")
    assert (PROFILES["TERRA_VERY_HIGH"]["runtime_model_id"], PROFILES["TERRA_VERY_HIGH"]["effort"]) == (
        "gpt-5.6-terra", "xhigh")
    assert (PROFILES["TERRA_MAX"]["runtime_model_id"], PROFILES["TERRA_MAX"]["effort"]) == ("gpt-5.6-terra", "max")
    for pid, effort in (("CLAUDE_SONNET_5_5_MEDIUM", "medium"), ("CLAUDE_SONNET_5_5_HIGH", "high")):
        assert (PROFILES[pid]["runtime_model_id"], PROFILES[pid]["effort"]) == ("claude-sonnet-5-5", effort)
    from model_catalog import validate_model_effort
    for pid in DEFAULT_CHAIN:
        validate_model_effort(PROFILES[pid]["runtime_model_id"], PROFILES[pid]["effort"])


def test_the_system_default_chain_is_luna6_then_terra56_then_sonnet55():
    catalog = pr.builtin_catalog()
    assert catalog["default_implementer_chain"]["profiles"] == DEFAULT_CHAIN
    assert catalog["choices"]["implementation"]["default"] == "RECOMMENDED"
    assert catalog["choices"]["implementation"]["options"]["RECOMMENDED"]["implementer_chain"] == "DEFAULT"


# ── policy ───────────────────────────────────────────────────────────────────

def test_validate_chain_rejects_empty_duplicate_and_oversized_chains():
    assert ap.validate_chain(["A", " B "]) == ["A", "B"]
    for bad in (None, [], "A", ["A", "A"], ["A", ""], ["A", 3], [f"P{i}" for i in range(ap.MAX_CHAIN_LENGTH + 1)]):
        with pytest.raises(ValueError):
            ap.validate_chain(bad)


def test_slots_follow_the_chain_and_clamp_to_its_length():
    one = ap.chain_slot_ids(["X"])
    assert set(one.values()) == {"X"} and set(one) == set(ap.CHAIN_SLOT_INDEX)
    two = ap.chain_slot_ids(["X", "Y"])
    assert (two["implementer_default"], two["implementer_harder"], two["implementer_hard"],
            two["implementer_capability_escalation"], two["repair_default"]) == ("X", "Y", "Y", "Y", "Y")
    full = ap.chain_slot_ids(DEFAULT_CHAIN)
    assert full["implementer_default"] == "GPT6_LUNA_HIGH" and full["implementer_harder"] == "GPT6_LUNA_VERY_HIGH"
    assert full["implementer_hard"] == "GPT6_LUNA_MAX" and full["repair_default"] == "GPT6_LUNA_VERY_HIGH"
    assert full["repair_hard"] == "GPT6_LUNA_MAX" and full["implementer_capability_escalation"] == "TERRA_HIGH"


def test_complexity_picks_the_starting_step_and_a_single_model_chain_pins_one_model():
    chain = DEFAULT_CHAIN
    assert [ap.select_implementation(IDS, c, chain=chain)["profile_id"]
            for c in ("NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT")] == chain[:3]
    pinned = ["TERRA_MAX"]
    assert {ap.select_implementation(IDS, c, chain=pinned)["profile_id"]
            for c in ("NORMAL", "HARDER", "SIGNIFICANTLY_DIFFICULT")} == {"TERRA_MAX"}


def test_a_concrete_capability_failure_climbs_one_step_and_never_past_the_last():
    chain = DEFAULT_CHAIN
    for index in range(len(chain) - 1):
        result = ap.select_implementation(IDS, "NORMAL", previous_attempt=failed(chain, index), chain=chain)
        assert result["profile_id"] == chain[index + 1] and result["escalated_from"] == chain[index]
        assert result["selection_reason"] == "CHAIN_CAPABILITY_FAILURE" and result["chain_index"] == index + 1
    last = ap.select_implementation(IDS, "NORMAL", previous_attempt=failed(chain, len(chain) - 1), chain=chain)
    assert last["profile_id"] == chain[0] and last["selection_reason"] == "DEFAULT_IMPLEMENTATION"


def test_a_weak_signal_does_not_escalate_the_chain():
    chain = DEFAULT_CHAIN
    for patch in ({"outcome": "PASSED"}, {"finding_code": "OTHER"}, {"execution_id": ""}, {"evidence_ref": ""},
                  {"profile_id": "NOT_IN_CHAIN"}):
        previous = {**failed(chain, 0), **patch}
        assert ap.select_implementation(IDS, "NORMAL", previous_attempt=previous,
                                        chain=chain)["profile_id"] == chain[0]


def test_repair_climbs_the_chain_only_on_a_blocking_capability_finding_with_evidence():
    chain = DEFAULT_CHAIN
    result = ap.select_repair(IDS, attempt=1, findings=[CAPABILITY], previous_attempt=failed(chain, 2), chain=chain)
    assert result["profile_id"] == chain[3] and result["escalated_from"] == chain[2]
    plain = ap.select_repair(IDS, attempt=1, findings=[{"finding_key": "F", "blocking": True}],
                             previous_attempt=failed(chain, 2), chain=chain)
    assert plain["profile_id"] == IDS["repair_default"]
    no_evidence = {**CAPABILITY, "evidence_ref": ""}
    assert ap.select_repair(IDS, attempt=1, findings=[no_evidence], previous_attempt=failed(chain, 2),
                            chain=chain)["profile_id"] == IDS["repair_default"]


def test_final_review_is_not_made_hard_by_a_short_or_low_chain_but_is_after_step_four():
    base = dict(changed_files=["src/a.py"], repair_attempts=0, findings=[])
    assert ap.select_final_review(IDS, implementation_profile_id="X", chain=["X"], **base)["tier"] == "DEFAULT"
    assert ap.select_final_review(IDS, implementation_profile_id=DEFAULT_CHAIN[2], chain=DEFAULT_CHAIN,
                                  **base)["tier"] == "DEFAULT"
    hard = ap.select_final_review(IDS, implementation_profile_id=DEFAULT_CHAIN[3], chain=DEFAULT_CHAIN, **base)
    assert hard["tier"] == "HARD" and "IMPLEMENTATION_ESCALATED_BEYOND_LUNA_MAX" in hard["complexity_risk_evidence"]


def test_repair_ladder_walks_the_chain_and_validates_for_any_chain_length():
    for n in (1, 2, 3, 4, 8, ap.MAX_CHAIN_LENGTH):
        chain = [f"P{i}" for i in range(n)]
        cfg = ap.default_repair_escalation(IDS, chain)
        rx.normalize_config(cfg)                                  # never rejected, whatever the length
        assert cfg["effort_ladder"] == chain and cfg["roles"]["difficult_implementer"] == chain[-1]
        assert 1 <= cfg["max_effort_steps"] <= 6
    legacy = ap.default_repair_escalation(IDS)
    assert legacy["roles"]["difficult_implementer"] == IDS["implementer_capability_escalation"]


# ── contract ─────────────────────────────────────────────────────────────────

def role_config(chain):
    config = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    config.pop("repair_escalation", None)                         # derived from the chain
    config["implementer_chain"] = chain
    config["policy_profiles"].update(ap.chain_slot_ids(chain))
    slots = ap.chain_slot_ids(chain)
    for role, slot in (("implementer", "implementer_default"), ("self_verifier", "implementer_default"),
                       ("review_prep", "review_pretreatment"), ("repairer", "repair_default")):
        config["roles"][role] = {"profile_id": slots[slot]}
    return config


def test_validate_roles_resolves_chain_bindings_and_rejects_bad_chains():
    resolved = ac.validate_roles(role_config(DEFAULT_CHAIN), PROFILES)
    assert [b["profile_id"] for b in resolved["implementer_chain"]] == DEFAULT_CHAIN
    assert resolved["implementer_chain"][3]["runtime_model_id"] == "gpt-5.6-terra"
    assert "implementer_chain" in ac.NON_ROLE_KEYS
    assert {"terra_high", "gpt-5.6-terra", "claude-sonnet-5-5"} <= ac.agent_identities(resolved)
    with pytest.raises(ac.AutonomyError, match="unknown profiles"):
        ac.validate_roles(role_config([*DEFAULT_CHAIN[:4], "NO_SUCH_PROFILE"]), PROFILES)
    repeated = role_config(["TERRA_HIGH", "TERRA_MAX"])
    repeated["implementer_chain"] = ["TERRA_HIGH", "TERRA_HIGH"]
    with pytest.raises(ac.AutonomyError, match="repeat"):
        ac.validate_roles(repeated, PROFILES)
    config = role_config(DEFAULT_CHAIN)
    config.pop("policy_profiles")
    with pytest.raises(ac.AutonomyError, match="policy_profiles"):
        ac.validate_roles(config, PROFILES)


def test_roles_without_a_chain_are_unchanged():
    resolved = ac.load_roles(ROOT / "AUTONOMY_ROLES.json", ROOT / "IMPLEMENTER_PROFILES.json")
    assert "implementer_chain" not in resolved


# ── product resolution ───────────────────────────────────────────────────────

def test_default_choice_uses_the_system_chain_when_every_step_runs():
    res = pr.resolve_choices({}, runnable=ALL_RUNNABLE)
    assert res["implementer_chain"]["source"] == "DEFAULT"
    assert [s["profile_id"] for s in res["implementer_chain"]["steps"]] == DEFAULT_CHAIN
    assert res["roles_config"]["implementer_chain"] == DEFAULT_CHAIN
    assert not res["blockers"] and res["roles_valid"]
    assert res["slots"]["implementer_default"]["status"] == "RECOMMENDED"
    assert res["slots"]["implementer_capability_escalation"]["profile_id"] == "TERRA_HIGH"
    summary = pr.group_summary(res)["implementation"]
    assert summary["models"] == [s["display"] for s in res["implementer_chain"]["steps"]]


def test_default_chain_skips_unrunnable_later_steps_visibly_and_never_substitutes():
    res = pr.resolve_choices({}, runnable=CODEX_RUNNABLE, states={
        "CLAUDE_SONNET_5_5_MEDIUM": {"state": "NEEDS_CHECK", "reason": "needs a probe"},
        "CLAUDE_SONNET_5_5_HIGH": {"state": "NEEDS_CHECK", "reason": "needs a probe"}})
    steps = [s["profile_id"] for s in res["implementer_chain"]["steps"]]
    assert steps == ["GPT6_LUNA_HIGH", "GPT6_LUNA_VERY_HIGH", "GPT6_LUNA_MAX", "TERRA_HIGH"]      # Terra xhigh/max not here
    assert [s["profile_id"] for s in res["implementer_chain"]["skipped"]][-2:] == [
        "CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH"]
    assert any("Pominięto" in w for w in res["warnings"])
    assert pr.group_summary(res)["implementation"]["checkable"] == [
        "CLAUDE_SONNET_5_5_HIGH", "CLAUDE_SONNET_5_5_MEDIUM"]


def test_default_chain_is_not_used_when_its_first_step_is_unavailable():
    without_start = ALL_RUNNABLE - {"GPT6_LUNA_HIGH"}
    res = pr.resolve_choices({}, runnable=without_start)
    assert res["implementer_chain"]["source"] == "SLOTS"          # the visible per-slot candidates decide
    assert res["slots"]["implementer_default"]["profile_id"] == "SONNET_HIGH"
    assert res["slots"]["implementer_default"]["status"] == "ALTERNATIVE"
    only_codex = pr.resolve_choices({}, runnable=CODEX_RUNNABLE - {"GPT6_LUNA_HIGH"})
    assert any("Implementacja" in b for b in only_codex["blockers"])


def test_user_chain_in_the_users_order_is_kept_exactly():
    mine = ["TERRA_MAX", "CLAUDE_SONNET_5_5_HIGH", "GPT6_LUNA_HIGH"]
    res = pr.resolve_choices({}, runnable=ALL_RUNNABLE, implementer_chain=mine)
    assert res["implementer_chain"]["source"] == "USER" and not res["blockers"]
    assert [s["profile_id"] for s in res["implementer_chain"]["steps"]] == mine
    assert res["roles_config"]["implementer_chain"] == mine
    assert res["roles_config"]["roles"]["implementer"]["profile_id"] == "TERRA_MAX"
    assert res["slots"]["implementer_default"]["status"] == "OVERRIDE"
    assert res["slots"]["implementer_hard"]["profile_id"] == "GPT6_LUNA_HIGH"
    assert res["roles_valid"]


def test_any_single_model_can_be_the_implementer_for_everything():
    res = pr.resolve_choices({}, runnable=ALL_RUNNABLE, implementer_chain=["CLAUDE_SONNET_5_5_HIGH"])
    assert not res["blockers"] and res["roles_valid"]
    impl = {res["slots"][slot]["profile_id"] for slot in ap.CHAIN_SLOT_INDEX}
    assert impl == {"CLAUDE_SONNET_5_5_HIGH"}
    assert res["roles_config"]["implementer_chain"] == ["CLAUDE_SONNET_5_5_HIGH"]
    # an independent reviewer is still chosen
    assert res["slots"]["primary_reviewer"]["runtime_model_id"] != "claude-sonnet-5-5"


def test_user_chain_problems_block_start_with_a_clear_reason():
    def blockers(chain, runnable=ALL_RUNNABLE, **kw):
        return pr.resolve_choices({}, runnable=runnable, implementer_chain=chain, **kw)["blockers"]
    assert any("nieznane profile" in b for b in blockers(["GPT6_LUNA_HIGH", "NOPE"]))
    assert any("powtarza" in b or "repeat" in b for b in blockers(["TERRA_HIGH", "TERRA_HIGH"]))
    assert any("lokalne" in b for b in blockers(["LOCAL_QWEN_FAST"]))
    assert any("Implementacja" in b for b in blockers(["TERRA_MAX", "TERRA_HIGH"], ALL_RUNNABLE - {"TERRA_MAX"}))
    # the 2nd step also repairs and prepares review, so it has to run; later steps only warn
    assert any("Naprawa" in b for b in blockers(["TERRA_HIGH", "TERRA_MAX"], ALL_RUNNABLE - {"TERRA_MAX"}))
    late = pr.resolve_choices({}, runnable=ALL_RUNNABLE - {"CLAUDE_SONNET_5_5_HIGH"}, implementer_chain=[
        "TERRA_HIGH", "TERRA_VERY_HIGH", "TERRA_MAX", "GPT6_LUNA_HIGH", "CLAUDE_SONNET_5_5_HIGH"])
    assert not late["blockers"] and any("Krok 5 łańcucha implementatora" in w for w in late["warnings"])


def test_a_manual_slot_override_cannot_silently_fight_the_chain():
    res = pr.resolve_choices({}, runnable=ALL_RUNNABLE, implementer_chain=["TERRA_HIGH"],
                             overrides={"implementer_default": "GPT6_LUNA_HIGH"})
    assert any("koliduje z łańcuchem" in b for b in res["blockers"])
    other = pr.resolve_choices({}, runnable=ALL_RUNNABLE, implementer_chain=["TERRA_HIGH"],
                               overrides={"final_review_hard": "SOL_HIGH"})
    assert not other["blockers"]


def test_other_implementation_levels_keep_per_slot_candidates_without_a_chain():
    res = pr.resolve_choices({"implementation": "ECONOMIC"}, runnable=ALL_RUNNABLE)
    assert res["implementer_chain"]["source"] == "SLOTS" and "implementer_chain" not in res["roles_config"]
    assert res["slots"]["implementer_default"]["profile_id"] == "TERRA_HIGH"


# ── real controller driven through the chain ─────────────────────────────────

def chain_rig(tmp_path, chain, **kw):
    rig = Rig(tmp_path, roles=ac.validate_roles(role_config(chain), PROFILES), **kw)
    return rig


def with_complexity(complexity):
    def build(ctx):
        out = initial_plan(["A"])(ctx)
        out["implementation_complexity"] = complexity
        if complexity != "NORMAL":
            out["complexity_evidence"] = ["fixture: cross-module change"]
        return out
    return build


def execute_profile(state):
    return next(row["profile"] for row in state["executions"] if row["executor"] == "execute")


def test_controller_starts_on_the_chain_step_the_plan_complexity_selects(tmp_path):
    for complexity, expected in (("NORMAL", 0), ("HARDER", 1), ("SIGNIFICANTLY_DIFFICULT", 2)):
        rig = chain_rig(tmp_path / complexity, DEFAULT_CHAIN)
        rig.replace("plan", with_complexity(complexity))
        state = rig.controller().run()
        assert execute_profile(state) == DEFAULT_CHAIN[expected]
        assert state["status"] == ac.AWAITING_HUMAN


def test_controller_with_a_single_model_chain_uses_it_for_every_implementation_call(tmp_path):
    rig = chain_rig(tmp_path, ["TERRA_MAX"])
    rig.replace("plan", with_complexity("SIGNIFICANTLY_DIFFICULT"))
    state = rig.controller().run()
    used = {row["profile"] for row in state["executions"] if row["executor"] in ("execute", "self_verify")}
    assert used == {"TERRA_MAX"}


def test_controller_repair_climbs_the_chain_on_capability_failure_and_records_the_selection(tmp_path):
    chain = ["TERRA_HIGH", "TERRA_VERY_HIGH", "TERRA_MAX", "CLAUDE_SONNET_5_5_MEDIUM"]
    rig = chain_rig(tmp_path, chain, mandate=one_item(max_repair_attempts=3))
    rig.h.scripts["review"] = [{"verdict": "REPAIR_REQUIRED", "findings": [dict(CAPABILITY)]}]
    rig.script("repair", {"summary": "tried", "checks": ok_checks(), "changed_files": []})
    state = rig.controller().run()
    repairs = [row for row in state["executions"] if row["executor"] == "repair"]
    assert repairs, "a repair must have run"
    profiles = [row["profile"] for row in repairs]
    assert profiles[0] == chain[1]                                # execute ran chain[0]; its capability failure → step 2
    assert all(p in chain for p in profiles)
    assert [chain.index(p) for p in profiles] == sorted(chain.index(p) for p in profiles)   # never goes back down
    assert repairs[0]["selection"]["selection_reason"] == "CHAIN_CAPABILITY_FAILURE"
    assert repairs[0]["selection"]["escalated_from"] == chain[0]


def test_controller_resume_keeps_the_frozen_chain(tmp_path):
    rig = chain_rig(tmp_path, ["TERRA_HIGH", "TERRA_MAX"])
    controller = rig.controller()
    assert controller.implementer_chain == ["TERRA_HIGH", "TERRA_MAX"]
    assert controller.state["roles"]["implementer_chain"][1]["profile_id"] == "TERRA_MAX"
    state = controller.run()
    assert state["roles"]["implementer_chain"][0]["runtime_model_id"] == "gpt-5.6-terra"
