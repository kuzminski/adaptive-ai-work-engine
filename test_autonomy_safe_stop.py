"""Cooperative safe stop in AutonomyController.run (Product MVP V0.1 engine hook).

The hook only honours an ambient `run_cancellation` token; without one the
controller behaves exactly as before (the whole V0.1–V0.3 suite runs without
a token). These tests use scripted executors and the existing harness.
"""

import run_cancellation as rc
import autonomy_contract as ac
from test_autonomy import Harness, mandate_fixture, plan


def one_item_harness(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    return Harness(tmp_path, mandate=m).defaults().script("plan", plan(["A"]))


def ledger_close(c, execution_id):
    entry = c.ledger.lifecycle()[execution_id]
    return entry["closed"][-1]["payload"] if entry["closed"] else None


def test_no_token_means_no_pause(tmp_path):
    state = one_item_harness(tmp_path).controller().run()
    assert state["status"] == ac.AWAITING_HUMAN


def test_stop_requested_before_a_boundary_pauses_cleanly_and_resume_continues(tmp_path):
    h = one_item_harness(tmp_path)
    token = rc.CancellationToken("RUN1")
    original = h.scripts["execute"][0]

    def execute_then_stop(ctx):
        token.request("user pressed STOP SAFELY")   # no child is live: nothing is killed
        return original
    h.scripts["execute"] = [execute_then_stop]
    c = h.controller()
    with rc.cancellation_scope(token):
        state = c.run()
    assert state["status"] == ac.RUNNING and state["phase"] == ac.SELF_VERIFY and state["in_flight"] is None
    paused = c.journal.by_type("RUN_PAUSED")
    assert len(paused) == 1 and paused[0]["payload"]["resume_phase"] == ac.SELF_VERIFY
    assert not c.lock.held and not (c.dir / "controller.lock").exists()
    execute = [e for e in state["executions"] if e["executor"] == "execute"][0]
    assert ledger_close(c, execute["execution_id"])["close_reason"] == "COMPLETED"
    resumed = h.controller(resume=True)
    final = resumed.run()
    assert final["status"] == ac.AWAITING_HUMAN and final["hold"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED
    assert h.calls.count("execute") == 1


def test_cancelled_read_only_call_is_closed_cancelled_and_replayed_with_new_id(tmp_path):
    h = one_item_harness(tmp_path)

    def cancelled_review(ctx):
        raise rc.RunCancelled("STOP SAFELY", at_boundary="after_child:claude")
    h.scripts["review"] = [cancelled_review, {"verdict": "PASS", "summary": "fine"}]
    c = h.controller()
    state = c.run()
    flight = state["in_flight"]
    assert state["status"] == ac.RUNNING and flight["phase"] == ac.REVIEW
    close = ledger_close(c, flight["execution_id"])
    assert close["close_reason"] == "CANCELLED" and close["outcome"] == "CANCELLED_BY_REQUEST"
    event = c.journal.by_type("RUN_CANCELLED_IN_FLIGHT")[0]["payload"]
    assert event["execution_id"] == flight["execution_id"] and event["side_effect_phase"] is False
    resumed = h.controller(resume=True)
    assert resumed.journal.by_type("IN_FLIGHT_RECONCILED")[-1]["payload"]["decision"] == \
        "REPLAY_READ_ONLY_PHASE_WITH_NEW_EXECUTION_ID"
    final = resumed.run()
    assert final["status"] == ac.AWAITING_HUMAN
    reviews = [e for e in final["executions"] if e["executor"] == "review"]
    assert reviews[-1]["retry_of_execution_id"] == flight["execution_id"]


def test_cancelled_side_effect_call_escalates_on_resume_never_replays(tmp_path):
    h = one_item_harness(tmp_path)

    def cancelled_execute(ctx):
        raise rc.RunCancelled("FORCE STOP", at_boundary="after_child:claude")
    h.scripts["execute"] = [cancelled_execute]
    c = h.controller()
    state = c.run()
    assert state["in_flight"]["phase"] == ac.EXECUTE
    assert c.journal.by_type("RUN_CANCELLED_IN_FLIGHT")[0]["payload"]["side_effect_phase"] is True
    final = h.controller(resume=True).state
    assert final["status"] == ac.AWAITING_HUMAN
    assert final["escalation"]["code"] == ac.E_INTERRUPTED and not final["hold"]["promotable"]
    assert h.calls.count("execute") == 1


def test_pre_dispatch_cancellation_records_confirmed_no_effect(tmp_path):
    h = one_item_harness(tmp_path)

    def refused_before_spawn(ctx):
        raise rc.RunCancelled("STOP", at_boundary="before_spawn:claude")
    h.scripts["final_review"] = [refused_before_spawn]
    c = h.controller()
    state = c.run()
    close = ledger_close(c, state["in_flight"]["execution_id"])
    assert close["effect_certainty"] == "CONFIRMED" and close["observation_source"] == "PRE_DISPATCH_FAILURE"


# ── initial architect handoff (found by the first live product walkthrough) ──

def test_initial_architect_handoff_names_the_mandatory_gate_codes_and_a_valid_template(tmp_path):
    """Live claude-opus-5 paraphrased the Human Gate conditions because the
    handoff never named the mandatory codes; the validator (correctly) rejected
    the charter. The handoff now carries the codes and the verbatim template."""
    import autonomy_adapters as aa
    mandate = ac.validate_mandate(mandate_fixture())
    ctx = {"mandate": mandate, "role": "initial_planner", "iteration_index": 1,
           "planning_stage": "INITIAL_ARCHITECT", "roadmap": {}, "history": [], "env": None,
           "execution": {"run_id": "R", "iteration_id": "I", "execution_id": "E", "descriptor_path": "d"}}

    class Env:
        def describe(self):
            return {"worktree": str(tmp_path)}
    ctx["env"] = Env()
    handoff = aa.build_handoff("plan", ctx)
    assert handoff["REQUIRED_HUMAN_GATE_CONDITIONS"] == list(ac.REQUIRED_CHARTER_GATE_CONDITIONS)
    template = handoff["DIRECTIONAL_CHARTER_TEMPLATE"]
    assert ac.validate_directional_charter({**template, "risk_guidance": []}, mandate)["charter_hash"]
    paraphrased = {**template, "risk_guidance": [], "human_gate_conditions": ["Integration is reserved for a human"]}
    try:
        ac.validate_directional_charter(paraphrased, mandate)
    except ac.AutonomyError as exc:
        assert "Human Gate" in str(exc)
    else:
        raise AssertionError("a paraphrased gate list must still be rejected")
    assert "REQUIRED_HUMAN_GATE_CONDITIONS" in aa.ROLE_INSTRUCTIONS["plan"]
    later = aa.build_handoff("plan", {**ctx, "planning_stage": "NEXT_ITERATION_PLAN", "iteration_index": 2})
    assert "DIRECTIONAL_CHARTER_TEMPLATE" not in later


def test_planner_handoff_carries_the_decision_vocabulary_and_skip_semantics():
    """Second live finding: the planner invented decision kinds ('scope_selection') and
    'skipped' a direction item that was only waiting for its dependency."""
    import autonomy_adapters as aa
    mandate = ac.validate_mandate(mandate_fixture())

    class Env:
        def describe(self):
            return {"worktree": "."}
    ctx = {"mandate": mandate, "role": "planner", "iteration_index": 2, "planning_stage": "NEXT_ITERATION_PLAN",
           "roadmap": {}, "history": [], "env": Env(),
           "execution": {"run_id": "R", "iteration_id": "I", "execution_id": "E", "descriptor_path": "d"}}
    kinds = aa.build_handoff("plan", ctx)["DECISION_KINDS"]
    assert kinds == ac.LEVEL_BY_KIND and kinds["LOCAL_TECHNICAL"] == ac.AUTO
    assert ac.classify_decision("scope_selection") == ac.ESCALATE      # unknown kinds still escalate
    assert "DECISION_KINDS" in aa.ROLE_INSTRUCTIONS["plan"]
    assert "PERMANENTLY" in aa.ROLE_INSTRUCTIONS["plan"]
