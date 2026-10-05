"""AAW AUTONOMOUS ITERATIONS V0.2 — execution identity, run lock, resume
reconciliation, real-adapter path and Human Gate hardening.

Two kinds of executor are used:

* scripted executors (the V0.1 convention) for state-machine level cases;
* the REAL `autonomy_adapters.DirectRoleExecutor` driving a scripted stand-in
  for the `claude` binary (`aaw_autonomy_fake_cli.py`). That exercises real
  argv, stdin handoff, a real child process (PID + OS creation time in
  EXECUTION_STARTED), descriptor update, result artifact and close — only the
  model is scripted.
"""

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import autonomy_adapters as aa
import autonomy_contract as ac
import autonomy_controller as ctl
import autonomy_run_lock as rl
import execution_ledger as el
import workflow_runner as wr
from test_autonomy import FakeEnv, Harness, mandate_fixture, ok_checks, plan

ROOT = Path(__file__).parent
EXE = re.compile(r"^EXE_[0-9a-f]{32}$")
ITER = re.compile(r"^ITER_[0-9a-f]{32}$")


class Crash(BaseException):
    """Process death: not an Exception, so nothing in the controller can swallow it."""


def ledger(h, run="RUN1"):
    return el.ExecutionLedger.for_run(run, h.stats)


def journal(h, run="RUN1"):
    return ctl.AutonomyJournal(h.stats / run / "AUTONOMY" / "autonomy_events.jsonl", run).read()


def single_item_mandate():
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    return m


# ── execution identity and the evidence boundary ─────────────────────────────

def test_every_role_call_is_one_v04a_execution_with_ledger_lifecycle(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    state = h.controller().run()
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"]
    executions = state["executions"]
    assert [e["executor"] for e in executions] == ["plan", "execute", "self_verify", "review", "final_review"]
    ids = [e["execution_id"] for e in executions]
    assert all(EXE.match(i) for i in ids) and len(set(ids)) == len(ids)
    life = ledger(h).lifecycle()
    for e in executions:
        descriptor = json.loads(Path(e["descriptor_path"]).read_text(encoding="utf-8"))
        assert descriptor["execution_id"] == e["execution_id"] and descriptor["run_id"] == "RUN1"
        assert descriptor["relations"]["iteration_id"] == e["iteration_id"]
        assert descriptor["profile"] == e["profile"] and descriptor["node_id"].startswith(f"AUTONOMY:{e['iteration_id']}:")
        entry = life[e["execution_id"]]
        assert entry["intent"] and entry["state"] == "CLOSED"
        # scripted executors spawn nothing: closed honestly, without a fabricated start
        assert not entry["started"] and entry["closed"][0]["payload"]["observation_source"] == "IN_PROCESS_ADAPTER_RETURN"
    kinds = {e["executor"]: json.loads(Path(e["descriptor_path"]).read_text())["invocation_kind"] for e in executions}
    assert kinds == {"plan": "PLAN", "execute": "LLM", "self_verify": "LLM", "review": "REVIEW", "final_review": "REVIEW"}
    review = next(e for e in executions if e["executor"] == "review")
    rel = json.loads(Path(review["descriptor_path"]).read_text())["relations"]
    assert rel["reviewed_execution_ids"] == [ids[1]]
    report = el.validate_ledger(ledger(h).path, "RUN1")
    assert report["valid"] and not report["unresolved"]["intent_without_started"] \
        and not report["unresolved"]["started_without_closed"]


def test_autonomy_journal_references_execution_ids_but_never_states_lifecycle(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    state = h.controller().run()
    events = journal(h)
    known = {e["execution_id"] for e in state["executions"]}
    started = [e for e in events if e["event_type"] == "PHASE_STARTED"]
    assert started and all(e["payload"]["execution_id"] in known for e in started)
    verdicts = [e for e in events if e["event_type"] == "REVIEW_VERDICT"]
    assert verdicts and all(v["payload"]["execution_id"] in known for v in verdicts)
    assert not {e["event_type"] for e in events} & set(el.EVENT_TYPES)
    text = json.dumps(events)
    for lifecycle_fact in ("process_id", "process_creation_time", "observed_start_time", "observed_close_time",
                           "close_reason", "effect_certainty"):
        assert lifecycle_fact not in text
    completed = [e for e in events if e["event_type"] == "PHASE_COMPLETED"]
    by_phase = {e["phase"]: e["payload"]["execution_id"] for e in completed}
    assert by_phase[ac.AWAITING_REVIEW] is None and by_phase[ac.ROADMAP_CHECK] is None  # controller-only phases
    assert all(by_phase[p] in known for p in (ac.PLAN, ac.EXECUTE, ac.SELF_VERIFY, ac.REVIEW, ac.FINAL_REVIEW))
    # and the ledger, conversely, carries the shared keys
    intents = [e for e in ledger(h).events() if e["event_type"] == el.EXECUTION_INTENT]
    assert {e["execution_id"] for e in intents} == known
    assert all(e["payload"]["autonomy_iteration_id"] for e in intents)


def test_iteration_identity_is_a_stable_uuid_distinct_from_index_and_execution(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    state = h.controller().run()
    its = [i["iteration_id"] for i in state["iterations"]]
    assert all(ITER.match(i) for i in its) and len(set(its)) == 3
    assert not set(its) & {e["execution_id"] for e in state["executions"]}
    for it in state["iterations"]:
        mine = [e for e in state["executions"] if e["iteration_id"] == it["iteration_id"]]
        assert [e["executor"] for e in mine][:2] == ["plan", "execute"] and it["plan_execution_id"] == mine[0]["execution_id"]


def test_multi_iteration_same_run_distinct_ids_frozen_mandate_and_budgets(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "first"}, {"item_id": "B", "title": "second"}]
    h = Harness(tmp_path, mandate=m).defaults().script("plan", plan(["A"]), plan(["B"]))
    state = h.controller().run()
    assert [i["outcome"] for i in state["iterations"]] == ["PASS", "PASS"]
    assert state["hold"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED and state["status"] == ac.AWAITING_HUMAN
    assert {i["lineage"]["mandate_hash"] for i in state["iterations"]} == {state["mandate_hash"]}
    assert ac.mandate_hash_ok(state["mandate"])
    assert state["mandate"]["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] == 5
    assert all(i["repair_attempts"] == 0 for i in state["iterations"])
    decisions = [e["payload"] for e in journal(h) if e["event_type"] == "ROADMAP_DECISION"]
    assert [(d["iterations_done"], d["next_action_available"]) for d in decisions] == [(1, True), (2, False)]
    # each iteration's executions are its own; nothing is shared across iterations
    by_it = {}
    for e in state["executions"]:
        by_it.setdefault(e["iteration_id"], set()).add(e["execution_id"])
    a, b = by_it.values()
    assert not a & b and state["hold"]["execution_ids"] == [e["execution_id"] for e in state["executions"]]


def test_reused_provider_session_is_not_fresh_context_and_escalates(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))

    def with_session(name, result):
        def run(ctx):
            from execution_contract import update_execution
            update_execution(ctx["execution"]["descriptor_path"], ctx["execution"]["execution_id"],
                             provider_session_id="SESSION-SHARED")
            return result
        return run
    h.scripts["execute"] = [with_session("execute", {"summary": "implemented", "checks": ok_checks()})]
    h.scripts["self_verify"] = [with_session("self_verify", {"checks": ok_checks()})]
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_SESSION_REUSE and not state["hold"]["promotable"]


def test_intent_write_failure_dispatches_nothing(tmp_path, monkeypatch):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    c = h.controller()

    def broken(**kwargs):
        raise el.LedgerWriteError("disk full")
    monkeypatch.setattr(c.ledger, "record_execution_intent", broken)
    state = c.run()
    assert state["escalation"]["code"] == ac.E_LEDGER and h.calls == []


def test_self_verify_must_not_change_the_candidate(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))

    def sneaky(ctx):
        ctx["env"].diff_text += "+edited during verification\n"
        return {"checks": ok_checks()}
    h.scripts["self_verify"] = [sneaky]
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_VERIFY_MUTATION and h.calls.count("review") == 0


def test_reviewer_packet_carries_the_required_review_material(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    h.controller().run()
    ctx = h.ctxs["review"][0]
    raw = ctx["raw"]
    assert ctx["mandate"]["mandate_hash"] and ctx["iteration"]["plan"]["goal"]
    assert raw["base_head"] == "base0" and raw["head"] == "h1" and raw["changed_files"]
    assert Path(raw["diff_path"]).read_text(encoding="utf-8") == h.env.diff_text
    assert "diff" not in raw
    assert raw["self_verify"] and raw["implementation"]["summary"] == "implemented"
    assert raw["previous_findings"] == [] and "commits" in raw and "commits" in ctx["packet"]["access"]
    assert "history" not in ctx and "transcript" not in json.dumps(ctx["packet"])


# ── A–I failure cases ───────────────────────────────────────────────────────

def test_A_unavailable_planner_profile_blocks_without_substitution(tmp_path):
    roles = ac.load_roles(ROOT / "AUTONOMY_ROLES.json", ROOT / "IMPLEMENTER_PROFILES.json")
    roles.pop("policy_profiles", None)  # retain the V0.2 role-level preflight case
    roles["planner"] = {**roles["planner"], "profile_id": "FABLE_HIGH"}  # KNOWN_BUT_UNAVAILABLE in the catalog
    h = Harness(tmp_path, roles=roles)
    executors = aa.build_direct_executors()
    c = ctl.AutonomyController.start("RUN1", h.mandate, executors=executors, env=h.env, roles=roles, stats_root=h.stats)
    state = c.run()
    assert state["status"] == ac.AWAITING_HUMAN and state["escalation"]["code"] == ac.E_ROLE_UNAVAILABLE
    assert not state["hold"]["promotable"] and state["executions"] == []
    assert not (h.stats / "RUN1" / "EXECUTIONS").exists() and ledger(h).events() == []
    assert any(e["event_type"] == "ROLE_UNAVAILABLE" and e["payload"]["profile_id"] == "FABLE_HIGH" for e in journal(h))


def test_A_production_role_config_is_preflighted_honestly():
    roles = ac.load_roles(ROOT / "AUTONOMY_ROLES.json", ROOT / "IMPLEMENTER_PROFILES.json")
    report = aa.preflight_roles(roles)
    for role, row in report["roles"].items():
        if row["available"]:
            assert row["harness"] in aa.SUPPORTED_HARNESSES and row["reason"] is None
        else:
            assert row["reason"]  # an unavailable role always says why; nothing is substituted
            assert row["harness"] is None


def test_D_crash_during_plan_replays_read_only_under_a_new_execution_id(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults()
    armed = {"on": True}

    def crashing_plan(ctx):
        if armed.pop("on", False):
            raise Crash()
        return plan(["A"])(ctx)
    h.script("plan", crashing_plan, crashing_plan)
    with pytest.raises(Crash):
        h.controller().run()
    crashed = ctl.load_state("RUN1", h.stats)["in_flight"]
    assert crashed["phase"] == ac.PLAN and EXE.match(crashed["execution_id"])
    planning_id = ctl.load_state("RUN1", h.stats)["planning"]["iteration_id"]
    state = h.controller(resume=True).run()
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"]
    assert state["iterations"][0]["iteration_id"] == planning_id  # identity survived the replay
    replay = state["executions"][0]
    assert replay["executor"] == "plan" and replay["retry_of_execution_id"] == crashed["execution_id"]
    life = ledger(h).lifecycle()
    assert life[crashed["execution_id"]]["state"] == "INTENT_ONLY"  # never dispatched again, never "tidied"
    assert el.validate_ledger(ledger(h).path, "RUN1")["valid"]
    rec = [e for e in journal(h) if e["event_type"] == "IN_FLIGHT_RECONCILED"][0]["payload"]
    assert rec["decision"] == "REPLAY_READ_ONLY_PHASE_WITH_NEW_EXECUTION_ID" and rec["ledger_state"] == "INTENT_ONLY"


def test_E_crash_during_execute_is_not_replayed(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    h.scripts["execute"] = [lambda ctx: (_ for _ in ()).throw(Crash())]
    with pytest.raises(Crash):
        h.controller().run()
    state = h.controller(resume=True).run()
    assert state["escalation"]["code"] == ac.E_INTERRUPTED and h.calls.count("execute") == 1
    rec = [e for e in journal(h) if e["event_type"] == "IN_FLIGHT_RECONCILED"][0]["payload"]
    assert rec["decision"] == "ESCALATE_SIDE_EFFECT_PHASE" and EXE.match(rec["execution_id"])


def _spawn_then_crash(ctx):
    """A provider process really starts (spawn receipt → EXECUTION_STARTED), then the controller dies."""
    wr.run_process([sys.executable, "-c", "pass"], dispatch=True)
    raise Crash()


def test_H_started_but_never_closed_is_never_reinvoked_under_the_same_id(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    h.scripts["review"] = [_spawn_then_crash, {"verdict": "PASS"}]
    with pytest.raises(Crash):
        h.controller().run()
    orphan = ctl.load_state("RUN1", h.stats)["in_flight"]["execution_id"]
    assert ledger(h).lifecycle()[orphan]["state"] == "STARTED_NOT_CLOSED"
    state = h.controller(resume=True).run()
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"]
    intents = [e for e in ledger(h).events() if e["event_type"] == el.EXECUTION_INTENT and e["execution_id"] == orphan]
    assert len(intents) == 1 and ledger(h).lifecycle()[orphan]["state"] == "STARTED_NOT_CLOSED"
    replay = next(e for e in state["executions"] if e["executor"] == "review")
    assert replay["execution_id"] != orphan and replay["retry_of_execution_id"] == orphan
    with pytest.raises(el.LedgerDispatchError, match="AT_MOST_ONCE"):
        ledger(h).record_execution_intent(execution_id=orphan, node_id="x", invocation_kind="REVIEW")
    report = el.validate_ledger(ledger(h).path, "RUN1")
    assert report["valid"] and report["unresolved"]["started_without_closed"] == [orphan]


def test_H_started_never_closed_side_effect_phase_goes_to_a_human(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    h.scripts["execute"] = [_spawn_then_crash]
    with pytest.raises(Crash):
        h.controller().run()
    state = h.controller(resume=True).run()
    assert state["escalation"]["code"] == ac.E_INTERRUPTED and "STARTED_NOT_CLOSED" in state["escalation"]["detail"]
    assert h.calls.count("execute") == 1


def test_I_provider_result_recorded_but_state_write_lost_is_adopted_not_rerun(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    c = h.controller()
    original = c._record_execution_ref

    def die_before_state_write(ref):
        if ref["executor"] == "review":
            raise Crash()
        original(ref)
    c._record_execution_ref = die_before_state_write
    with pytest.raises(Crash):
        c.run()
    lost = ctl.load_state("RUN1", h.stats)["in_flight"]["execution_id"]
    assert ledger(h).close_status(lost)["close_reason"] == "COMPLETED"
    reviews_before = h.calls.count("review")
    state = h.controller(resume=True).run()
    assert h.calls.count("review") == reviews_before == 1  # not re-invoked
    adopted = [e for e in journal(h) if e["event_type"] == "EXECUTION_RESULT_ADOPTED"]
    assert [a["payload"]["execution_id"] for a in adopted] == [lost]
    assert state["iterations"][0]["reviews"][0]["execution_id"] == lost and state["hold"]["promotable"]
    assert sum(1 for e in state["executions"] if e["executor"] == "review") == 1


# ── run lock ────────────────────────────────────────────────────────────────

def _holder(lock_dir, ready, *, sleep=60):
    code = textwrap.dedent(f"""
        import sys, time, pathlib
        sys.path.insert(0, {str(ROOT)!r})
        import autonomy_run_lock as rl
        lock = rl.RunLock(pathlib.Path({str(lock_dir)!r}), "RUN1")
        lock.acquire()
        pathlib.Path({str(ready)!r}).write_text(lock.record["owner_token"])
        time.sleep({sleep})
    """)
    proc = subprocess.Popen([sys.executable, "-c", code])
    for _ in range(200):
        if Path(ready).exists():
            return proc, Path(ready).read_text()
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("lock holder did not start")


def test_F_second_controller_on_a_live_run_is_refused(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    first = h.controller()  # holds the run
    with pytest.raises(rl.RunLockError) as busy:
        h.controller(resume=True)
    assert busy.value.outcome == rl.RUN_LOCK_BUSY and busy.value.liveness == rl.OWNER_ALIVE
    first.run()
    assert not rl.lock_path(h.stats / "RUN1" / "AUTONOMY").exists()  # released at the end of run()


def test_F_controller_in_another_process_makes_the_run_busy(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    h.controller().release()
    proc, token = _holder(h.stats / "RUN1" / "AUTONOMY", tmp_path / "ready")
    try:
        with pytest.raises(rl.RunLockError) as busy:
            h.controller(resume=True)
        assert busy.value.outcome == rl.RUN_LOCK_BUSY and busy.value.owner["owner_token"] == token
        assert busy.value.owner["owner"]["process_creation_time"]  # PID is not identity on its own
        assert ctl.load_state("RUN1", h.stats)["phase"] == ac.PLAN  # nothing was touched
        assert any(e["event_type"] == rl.RUN_LOCK_BUSY for e in journal(h))
    finally:
        proc.kill()
        proc.wait()


def test_G_dead_owner_is_stale_but_never_taken_over_automatically(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    h.controller().release()
    run_dir = h.stats / "RUN1" / "AUTONOMY"
    proc, token = _holder(run_dir, tmp_path / "ready")
    proc.kill()
    proc.wait()
    before = rl.lock_path(run_dir).read_text()
    with pytest.raises(rl.RunLockError) as stale:
        h.controller(resume=True)
    assert stale.value.outcome == rl.RUN_LOCK_STALE and stale.value.liveness == rl.OWNER_DEAD
    assert rl.lock_path(run_dir).read_text() == before  # no takeover
    with pytest.raises(rl.RunLockError, match="not the one inspected"):
        rl.reconcile_stale_lock(run_dir, expected_owner_token="LCK_other", operator="kamil", reason="x")
    with pytest.raises(rl.RunLockError, match="named operator"):
        rl.reconcile_stale_lock(run_dir, expected_owner_token=token, operator=" ", reason="x")
    record = rl.reconcile_stale_lock(run_dir, expected_owner_token=token, operator="kamil",
                                     reason="controller process killed")
    assert record["retired_lock"]["owner_token"] == token
    assert (run_dir / f"controller.lock.retired.{token}.json").exists()
    assert h.controller(resume=True).run()["status"] == ac.AWAITING_HUMAN


def _forge(run_dir, owner):
    run_dir.mkdir(parents=True, exist_ok=True)
    rl.lock_path(run_dir).write_text(json.dumps({"schema_version": rl.SCHEMA_VERSION, "run_id": "RUN1",
                                                 "owner_token": "LCK_forged", "owner": owner}), encoding="utf-8")


@pytest.mark.parametrize("mutate, outcome, liveness", [
    (lambda o: {**o, "host": "another-machine"}, rl.RUN_LOCK_BUSY, rl.OWNER_UNKNOWN),
    (lambda o: {**o, "process_creation_time": None}, rl.RUN_LOCK_BUSY, rl.OWNER_UNKNOWN),
    (lambda o: {**o, "process_creation_time": "2001-01-01T00:00:00.000+00:00"}, rl.RUN_LOCK_STALE, rl.OWNER_PID_REUSED),
])
def test_G_ambiguous_owner_is_busy_and_pid_reuse_is_stale(tmp_path, mutate, outcome, liveness):
    run_dir = tmp_path / "AUTONOMY"
    me = rl.current_owner()
    if liveness == rl.OWNER_PID_REUSED and not me["process_creation_time"]:
        pytest.skip("platform exposes no process creation time")
    _forge(run_dir, mutate(me))
    found = rl.inspect_lock(run_dir)
    assert (found["outcome"], found["liveness"]) == (outcome, liveness)
    with pytest.raises(rl.RunLockError) as refused:
        rl.RunLock(run_dir, "RUN1").acquire()
    assert refused.value.outcome == outcome
    if outcome == rl.RUN_LOCK_BUSY:
        with pytest.raises(rl.RunLockError, match="not provably dead"):
            rl.reconcile_stale_lock(run_dir, expected_owner_token="LCK_forged", operator="kamil", reason="x")


def test_unreadable_lock_is_busy_not_stale(tmp_path):
    run_dir = tmp_path / "AUTONOMY"
    run_dir.mkdir()
    rl.lock_path(run_dir).write_text("{partial", encoding="utf-8")
    assert rl.inspect_lock(run_dir)["outcome"] == rl.RUN_LOCK_BUSY


def test_resume_takes_the_lock_before_reading_state(tmp_path, monkeypatch):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    h.controller().release()
    order = []
    real_acquire, real_load = rl.RunLock.acquire, ctl.load_state
    monkeypatch.setattr(rl.RunLock, "acquire", lambda self: order.append("lock") or real_acquire(self))
    monkeypatch.setattr(ctl, "load_state", lambda *a, **k: order.append("state") or real_load(*a, **k))
    h.controller(resume=True).release()
    assert order[:2] == ["lock", "state"]


# ── Human Gate ──────────────────────────────────────────────────────────────

def held(tmp_path):
    h = Harness(tmp_path, mandate=single_item_mandate()).defaults().script("plan", plan(["A"]))
    state = h.controller().run()
    return h, state["hold"]["candidate_id"]


def test_human_gate_wrong_approver_and_agent_roles_cannot_approve(tmp_path):
    h, cand = held(tmp_path)
    for who in ("", "  ", "repairer", "self_verifier", "GPT6_LUNA_HIGH", "gpt-6-luna"):
        with pytest.raises(ac.AutonomyError):
            ctl.approve_promotion("RUN1", approver=who, candidate_id=cand, stats_root=h.stats)
    with pytest.raises(ac.AutonomyError, match="HUMAN channel"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, channel="AGENT", stats_root=h.stats)
    with pytest.raises(ac.AutonomyError, match="candidate_id does not match"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id="CAND_other", stats_root=h.stats)


def test_human_gate_candidate_changed_after_approval_is_refused(tmp_path):
    h, cand = held(tmp_path)
    ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, stats_root=h.stats)
    h.env.diff_text += "+changed after approval\n"
    with pytest.raises(ac.AutonomyError, match="candidate changed after approval"):
        ctl.promote("RUN1", promoter=lambda t, s: {"status": "INTEGRATED"}, stats_root=h.stats, env=h.env)
    with pytest.raises(ac.AutonomyError, match="requires the workspace"):
        ctl.promote("RUN1", promoter=lambda t, s: {"status": "INTEGRATED"}, stats_root=h.stats)
    assert ctl.load_state("RUN1", h.stats)["status"] == ac.HUMAN_APPROVED


def test_human_gate_default_promote_merges_and_pushes_nothing(tmp_path):
    h, cand = held(tmp_path)
    ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, stats_root=h.stats)
    state = ctl.promote("RUN1", stats_root=h.stats)
    assert state["promotion"]["integration"] == {"status": "READY_FOR_EXTERNAL_INTEGRATION", "merged": False,
                                                 "pushed": False}
    assert state["promotion"]["candidate_id"] == cand and state["promotion"]["approved_candidate_fingerprint"]
    with pytest.raises(ac.AutonomyError):  # a second promote (token reuse at the state level) is refused
        ctl.promote("RUN1", stats_root=h.stats)


def test_promotion_token_is_bound_to_candidate_and_run_and_is_one_shot():
    ran = []
    runner = lambda argv: ran.append(list(argv)) or (0, "", "")
    wrong = ac.PromotionToken("RUN1", "APR_1", "CAND_other")
    with pytest.raises(ac.GitPolicyViolation):
        ac.GuardedGit(runner, token=wrong, candidate_id="CAND_x", run_id="RUN1").run(["git", "merge", "aaw/it"])
    other_run = ac.PromotionToken("RUN2", "APR_1", "CAND_x")
    with pytest.raises(ac.GitPolicyViolation):
        ac.GuardedGit(runner, token=other_run, candidate_id="CAND_x", run_id="RUN1").run(["git", "push", "origin", "main"])
    good = ac.PromotionToken("RUN1", "APR_1", "CAND_x")
    guarded = ac.GuardedGit(runner, token=good, candidate_id="CAND_x", run_id="RUN1")
    guarded.run(["git", "merge", "aaw/it"])
    with pytest.raises(ac.GitPolicyViolation):  # reused token
        guarded.run(["git", "push", "origin", "main"])
    assert ran == [["git", "merge", "aaw/it"]]


# ── the real adapter path, driven by a scripted provider binary ─────────────

@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """Put a scripted `claude` first on PATH and bind every role to a claude profile."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = ROOT / "aaw_autonomy_fake_cli.py"
    if os.name == "nt":
        (bin_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = bin_dir / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "PARENT-SESSION-MUST-NOT-LEAK")
    scenario = tmp_path / "scenario.json"
    monkeypatch.setenv("AAW_FAKE_CLI_SCENARIO", str(scenario))
    profiles = {p["profile_id"]: p for p in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
    cfg = {"allow_same_model_fresh_context": True,
           "roles": {r: {"profile_id": "SONNET_HIGH"} for r in ac.ROLES + tuple(ac.OPTIONAL_ROLE_ALIASES)}}
    roles = ac.validate_roles(cfg, profiles)

    def write(data):
        scenario.write_text(json.dumps(data), encoding="utf-8")

    def calls():
        path = scenario.with_name(scenario.name + ".calls.jsonl")
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
    return {"roles": roles, "write": write, "calls": calls}


PLAN_OUT = {"status": "ITERATION", "mandate_hash": "$MANDATE_HASH", "goal": "add the export module",
            "roadmap_refs": ["A"], "scope_justification": "item A of the roadmap",
            "acceptance_criteria": ["$ITERATION_CRITERIA"], "touched_areas": ["src/export"],
            "decisions": [{"kind": "TESTS", "summary": "unit tests"}], "skipped_items": [], "reason": None}
IMPL_OUT = {"summary": "implemented", "changed_files": ["src/export/core.py"],
            "checks": [{"name": "unit", "status": "PASS", "summary": "1 passed"}], "deviations": [], "uncertainties": []}
VERIFY_OUT = {"summary": "verified", "checks": [{"name": "unit", "status": "PASS", "summary": "1 passed"}]}
PASS_OUT = {"verdict": "PASS", "summary": "meets criteria", "findings": []}


class DirEnv(FakeEnv):
    """FakeEnv whose worktree is a real directory, so provider processes have a cwd."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def describe(self):
        return {"worktree": str(self.path), "base_head": "base0"}


def real_controller(tmp_path, fake, mandate=None):
    env = DirEnv(tmp_path / "wt")
    return ctl.AutonomyController.start("RUN1", mandate or single_item_mandate(), executors=aa.build_direct_executors(timeout=60),
                                        env=env, roles=fake["roles"], stats_root=tmp_path / "stats"), env


def test_real_adapter_path_records_spawn_session_and_close_per_role(tmp_path, fake_cli):
    fake_cli["write"]({"PLANNER": [{"output": PLAN_OUT}], "IMPLEMENTER": [{"output": IMPL_OUT,
                       "write_files": {"src/export/core.py": "def export():\n    return 1\n"}}],
                       "SELF-VERIFIER": [{"output": VERIFY_OUT}], "REVIEWER": [{"output": PASS_OUT}],
                       "FINAL REVIEWER": [{"output": PASS_OUT}]})
    c, env = real_controller(tmp_path, fake_cli)
    state = c.run()
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"], state["escalation"]
    assert (env.path / "src/export/core.py").exists()
    calls = fake_cli["calls"]()
    # No REVIEW-PRETREATMENT calls: the deterministic packet goes to the reviewers as-is (one model call per
    # review round saved; pretreatment could never change a verdict).
    assert [x["role"] for x in calls] == ["PLANNER", "IMPLEMENTER", "SELF-VERIFIER", "REVIEWER", "FINAL REVIEWER"]
    assert not any(x["inherited_session_env"] for x in calls)  # parent session id did not leak
    life = el.ExecutionLedger.for_run("RUN1", tmp_path / "stats").lifecycle()
    calls_by_execution = {call["execution_id"]: call for call in calls}
    for e in state["executions"]:
        call = calls_by_execution[e["execution_id"]]
        assert call["execution_id"] == e["execution_id"] and call["iteration_id"] == e["iteration_id"]
        entry = life[e["execution_id"]]
        start = entry["started"][0]["payload"]
        assert start["start_evidence"] == "CHILD_PROCESS_SPAWNED" and start["process_id"] > 0
        # On Windows the .cmd shim can spawn Python as a child; the ledger
        # observes cmd.exe while the fixture logs the inner Python PID.
        assert call["pid"] > 0
        close = entry["closed"][0]["payload"]
        assert close["observation_source"] == "CHILD_PROCESS_EXIT" and close["exit_code"] == 0
        assert e["provider_session_id"] == call["session_id"]
        artifact = json.loads(Path(close["result_refs"][0]).read_text())
        assert artifact["recorded_by"] == aa.ADAPTER_ID and artifact["model"] == "claude-sonnet-5"
    assert len({e["provider_session_id"] for e in state["executions"]}) == 5
    assert "MANDATE" in calls[0]["handoff_keys"] and "PACKET" in calls[3]["handoff_keys"]
    assert "RAW_EVIDENCE_MANIFEST" in calls[4]["handoff_keys"]
    assert "HISTORY" not in calls[4]["handoff_keys"]  # reviewers get packet + territory, not prior conversation
    assert el.validate_ledger(el.ledger_path_for_run("RUN1", tmp_path / "stats"), "RUN1")["valid"]


def test_B_reviewer_invalid_output_through_real_adapter_escalates(tmp_path, fake_cli):
    fake_cli["write"]({"PLANNER": [{"output": PLAN_OUT}], "IMPLEMENTER": [{"output": IMPL_OUT}],
                       "SELF-VERIFIER": [{"output": VERIFY_OUT}], "REVIEWER": [{"raw_text": "Looks good to me!"}]})
    c, _ = real_controller(tmp_path, fake_cli)
    state = c.run()
    assert state["escalation"]["code"] == ac.E_REVIEW_INVALID and not state["hold"]["promotable"]
    review = state["executions"][-1]
    close = el.ExecutionLedger.for_run("RUN1", tmp_path / "stats").close_status(review["execution_id"])
    assert close["outcome"] == "INVALID" and close["effect_certainty"] == "PARTIAL"


def test_C_identical_blocking_finding_after_bounded_repair_escalates_real_adapter(tmp_path, fake_cli):
    finding = {"finding_key": "F-EDGE", "severity": "HIGH", "summary": "edge case unhandled", "file": "src/export/core.py",
               "blocking": True, "evidence_ref": "RAW.diff"}
    repair_required = {"verdict": "REPAIR_REQUIRED", "summary": "fix", "findings": [finding]}
    fake_cli["write"]({"PLANNER": [{"output": PLAN_OUT}], "IMPLEMENTER": [{"output": IMPL_OUT}],
                       "SELF-VERIFIER": [{"output": VERIFY_OUT}], "REVIEWER": [{"output": repair_required}],
                       "FINAL REVIEWER": [{"output": repair_required}],
                       "REPAIRER": [{"output": {"summary": "tried", "addressed_findings": ["F-EDGE"],
                                                "changed_files": [], "checks": [], "uncertainties": []}}]})
    # V0.3 requires every repair to return through self-verification and primary
    # review. A repeated identical primary finding no longer stops after one repair: the
    # (here minimal, unconfigured) ladder re-diagnoses once on the same profile, then stops.
    c, _ = real_controller(tmp_path, fake_cli, mandate_fixture(max_repair_attempts=4) | {
        "roadmap_mandate": {**mandate_fixture(max_repair_attempts=4)["roadmap_mandate"],
                            "items": [{"item_id": "A", "title": "only"}]}})
    state = c.run()
    assert state["escalation"]["code"] == ac.E_NO_PROGRESS and state["iterations"][0]["repair_attempts"] == 2
    assert not state["hold"]["promotable"]
    assert [r["stage"] for r in state["iterations"][0]["repairs"]] == ["CURRENT", "EFFORT_UP"]
    roles = [x["role"] for x in fake_cli["calls"]()]
    assert roles.count("REPAIRER") == 2 and roles[-1] == "REVIEWER"
    c2, _ = real_controller(tmp_path / "limit", fake_cli)  # max_repair_attempts=2: the budget stops it first
    assert c2.run()["escalation"]["code"] in (ac.E_NO_PROGRESS, ac.E_REPAIR_LIMIT)


def test_provider_failure_through_real_adapter_closes_failed_and_escalates(tmp_path, fake_cli):
    fake_cli["write"]({"PLANNER": [{"exit_code": 3}]})
    c, _ = real_controller(tmp_path, fake_cli)
    state = c.run()
    assert state["escalation"]["code"] == ac.E_EXECUTOR
    e = state["executions"][0]
    close = el.ExecutionLedger.for_run("RUN1", tmp_path / "stats").close_status(e["execution_id"])
    assert close["close_reason"] == "FAILED" and close["exit_code"] == 3
