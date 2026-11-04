"""Opt-in controlled exploration: pure decision rules, config validation, and the controller applying them."""

import json
from pathlib import Path

import pytest

import aaw_experience as ex
import aaw_telemetry as tel
import autonomy_chain as chain
import autonomy_contract as ac
import autonomy_explore as explore
from test_autonomy import Harness, events, mandate_fixture
from test_autonomy_policy_v0_3 import continuation_plan, initial_plan, production_roles

ROOT = Path(__file__).parent
CFG = {"enabled": True, "max_percent": 50, "max_per_run": 1, "candidates": ["TERRA_HIGH"], "wanted": {"BUGFIX": ["TERRA_HIGH"]}}


# ── pure rules ───────────────────────────────────────────────────────────────

def decide(cfg=None, counters=None, **over):
    base = dict(kind="BUGFIX", tier="NORMAL", reason="DEFAULT_IMPLEMENTATION", iteration_index=3, critical=False,
                candidate_ok=lambda p: True)
    base.update(over)
    return explore.decide(explore.normalize_config({**CFG, **(cfg or {})}), counters or explore.empty_state(), **base)


def test_config_is_off_by_default_and_validated():
    assert explore.normalize_config(None)["enabled"] is False
    for bad in ({"max_percent": 0}, {"max_percent": 80}, {"max_per_run": 0}, {"enabled": "yes"}, {"typo": 1},
                {"candidates": "TERRA_HIGH"}, {"wanted": {"BUGFIX": ["NOT_A_CANDIDATE"]}}, {"first_explored_eligible": 1}):
        with pytest.raises(explore.ExplorationConfigError):
            explore.normalize_config({**CFG, **bad})
    with pytest.raises(explore.ExplorationConfigError, match="unknown profiles"):
        explore.normalize_config(CFG, known_profiles={"OTHER": {}})
    assert explore.period(explore.normalize_config({**CFG, "max_percent": 20})) == 5


@pytest.mark.parametrize("over,code", [
    ({"cfg": {"enabled": False}}, "DISABLED"),
    ({"tier": "HARDER"}, "NOT_ORDINARY_IMPLEMENTATION"),
    ({"reason": "HUMAN_OVERRIDE"}, "NOT_ORDINARY_IMPLEMENTATION"),
    ({"critical": True}, "CRITICAL_SCOPE"),
    ({"iteration_index": 1}, "FIRST_ITERATION"),
    ({"kind": "INFRA"}, "KIND_EXCLUDED"),
    ({"kind": "DOCS"}, "NO_THIN_CELL"),
    ({"counters": {"eligible": 1, "explored": 1, "decisions": {}}}, "BUDGET_EXHAUSTED"),
    ({"candidate_ok": lambda p: False, "counters": {"eligible": 1, "explored": 0, "decisions": {}}}, "NO_RUNNABLE_CANDIDATE"),
])
def test_exploration_never_happens_when_a_safety_rule_fails(over, code):
    out = decide(**over)
    assert out["explore"] is False and out["code"] == code and out["profile_id"] is None


def test_exploration_takes_every_nth_eligible_iteration_starting_at_the_configured_one():
    cfg = {"max_percent": 50, "max_per_run": 10, "first_explored_eligible": 2}
    counters, picks = explore.empty_state(), []
    for index in range(2, 10):
        out = decide(cfg, counters, iteration_index=index)
        counters["eligible"] = out["eligible_after"]
        picks.append(out["explore"])
    assert picks == [False, True, False, True, False, True, False, True]
    assert explore.period(explore.normalize_config({**CFG, **cfg})) == 2


def test_the_thinnest_runnable_candidate_wins():
    cfg = {"candidates": ["A", "B"], "wanted": {"BUGFIX": ["A", "B"]}}
    out = decide(cfg, {"eligible": 1, "explored": 0, "decisions": {}}, candidate_ok=lambda p: p == "B")
    assert out["explore"] and out["profile_id"] == "B"


# ── controller ───────────────────────────────────────────────────────────────

def explored_run(tmp_path, n_items=3, cfg=None, goal="Napraw błąd", bounds=None, **plan_over):
    m = mandate_fixture(max_iterations=20, **(bounds or {}))
    m["roadmap_mandate"]["items"] = [{"item_id": f"I{i}", "title": f"item {i}"} for i in range(n_items)]
    h = Harness(tmp_path, mandate=m)
    h.roles = production_roles()
    h.roles["chain"] = chain.normalize_config({"enabled": True, "length": 20, "plan_batching": False})
    h.roles["exploration"] = explore.normalize_config({**CFG, **(cfg or {})})
    h.defaults()

    def first(ctx):
        return {**initial_plan(["I0"])(ctx), "goal": f"{goal} 0", **plan_over}

    h.script("plan", first, *[continuation_plan([f"I{i}"], goal=f"{goal} {i}", **plan_over) for i in range(1, n_items)])
    return h, h.controller()


def execute_profiles(state):
    return [(e["profile"], (e.get("selection") or {}).get("selection_reason")) for e in state["executions"]
            if e["executor"] == "execute"]


def test_an_ordinary_iteration_is_implemented_by_the_candidate_and_everything_is_recorded(tmp_path):
    h, c = explored_run(tmp_path)
    state = c.run()
    assert state["escalation"] is None and state["status"] == ac.AWAITING_HUMAN
    assert execute_profiles(state) == [("GPT6_LUNA_HIGH", "DEFAULT_IMPLEMENTATION"), ("GPT6_LUNA_HIGH", "DEFAULT_IMPLEMENTATION"),
                                       ("TERRA_HIGH", "EXPLORATION")]
    assert state["exploration"]["explored"] == 1 and state["exploration"]["eligible"] == 2
    selected = events(c, "EXPLORATION_SELECTED")
    assert len(selected) == 1 and selected[0]["payload"]["profile_id"] == "TERRA_HIGH"
    assert selected[0]["payload"]["default_profile_id"] == "GPT6_LUNA_HIGH" and selected[0]["payload"]["kind"] == "BUGFIX"
    rows = ex.rows_from_state(state, tel.read_records(c.dir / "telemetry.jsonl"))
    assert [(r["profile_id"], r["explored"]) for r in rows] == [("GPT6_LUNA_HIGH", False), ("GPT6_LUNA_HIGH", False), ("TERRA_HIGH", True)]


def test_the_per_run_budget_is_a_hard_cap(tmp_path):
    h, c = explored_run(tmp_path, n_items=8, cfg={"max_percent": 50, "max_per_run": 2})
    state = c.run()
    assert sum(1 for _, why in execute_profiles(state) if why == "EXPLORATION") == 2
    assert state["exploration"]["explored"] == 2


def test_harder_iterations_critical_runs_and_disabled_config_are_never_explored(tmp_path):
    h, c = explored_run(tmp_path, implementation_complexity="HARDER", complexity_evidence=["wide change"])
    assert all(why != "EXPLORATION" for _, why in execute_profiles(c.run()))
    h, c = explored_run(tmp_path / "crit", bounds={"critical_scope": True})
    assert all(why != "EXPLORATION" for _, why in execute_profiles(c.run()))
    h, c = explored_run(tmp_path / "off", cfg={"enabled": False})
    state = c.run()
    assert all(why != "EXPLORATION" for _, why in execute_profiles(state)) and state["exploration"]["eligible"] == 0


def test_wrong_task_kind_is_not_explored(tmp_path):
    h, c = explored_run(tmp_path, goal="Uzupełnij README")
    assert all(why != "EXPLORATION" for _, why in execute_profiles(c.run()))


def test_an_unrunnable_candidate_is_skipped_without_stopping_the_run(tmp_path):
    h, c = explored_run(tmp_path)
    real = h.executors

    def with_preflight(**kwargs):
        executors = real(**kwargs)

        class Gate:
            def __init__(self, fn): self.fn = fn
            def __call__(self, ctx): return self.fn(ctx)
            @staticmethod
            def preflight(binding):
                return "not installed here" if binding.get("profile_id") == "TERRA_HIGH" else None
        executors["execute"] = Gate(executors["execute"])
        return executors

    h.executors = with_preflight
    c = h.controller("RUN_SKIP")
    state = c.run()
    assert state["escalation"] is None and all(why != "EXPLORATION" for _, why in execute_profiles(state))
    assert events(c, "EXPLORATION_SKIPPED")[0]["payload"]["code"] == "NO_RUNNABLE_CANDIDATE"


def test_a_candidate_sharing_the_reviewers_model_is_refused_when_independence_is_required(tmp_path):
    h, c = explored_run(tmp_path, cfg={"candidates": ["SOL_6_1_LIGHT"], "wanted": {"BUGFIX": ["SOL_6_1_LIGHT"]}})
    assert h.roles["reviewer"]["review_independence"] == "DIFFERENT_MODEL"
    assert all(why != "EXPLORATION" for _, why in execute_profiles(c.run()))


def test_a_decision_is_made_once_per_iteration_so_a_replay_cannot_change_it(tmp_path):
    h, c = explored_run(tmp_path)
    c.run()
    plan = c.state["iterations"][2]["plan"]
    before = json.loads(json.dumps(c.state["exploration"]))
    again = c._policy_selection("execute", {"plan": plan})           # the third iteration is the current one
    assert again["selection_reason"] == "EXPLORATION" and again["profile_id"] == "TERRA_HIGH"
    assert c.state["exploration"] == before                          # nothing was counted twice


def test_role_validation_rejects_unknown_candidates_and_the_shipped_roles_have_no_exploration():
    config = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    catalog = {p["profile_id"]: p for p in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
    assert "exploration" not in config and "exploration" not in ac.validate_roles(config, catalog)
    config["exploration"] = {**CFG, "candidates": ["NO_SUCH_PROFILE"], "wanted": {}}
    with pytest.raises(ac.AutonomyError, match="exploration config invalid"):
        ac.validate_roles(config, catalog)
    config["exploration"] = CFG
    assert ac.validate_roles(config, catalog)["exploration"]["enabled"] is True and "exploration" in ac.NON_ROLE_KEYS
