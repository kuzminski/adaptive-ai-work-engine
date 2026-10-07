"""AAW AUTONOMOUS ITERATIONS V0.1 — lifecycle, scope, authority and resume tests.

The controller is driven through its real executor boundary with scripted
executors (the same convention the workflow tests use for the LLM adapter).
Numbers in test names refer to the required-test list of the change request.
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest

import autonomy_contract as ac
import autonomy_controller as ctl

ROOT = Path(__file__).parent


# ───────────────────────────── fixtures ─────────────────────────────────────

def mandate_fixture(**bounds):
    return {
        "mandate_id": "M1",
        "iteration_contract": {
            "goal": "Add the export module", "scope": ["src/export"],
            "acceptance_criteria": ["export works", "tests added"],
            "constraints": ["no new dependencies"], "forbidden_changes": ["public API"],
            "required_evidence": ["unit tests"]},
        "roadmap_mandate": {
            "objective": "Reliable export", "priorities": ["correctness"],
            "items": [{"item_id": "A", "title": "core export"},
                      {"item_id": "B", "title": "csv format", "depends_on": ["A"]},
                      {"item_id": "C", "title": "docs"}],
            "autonomy_bounds": {"max_iterations": 5, "max_repair_attempts": 2,
                                "allowed_areas": ["src", "tests", "docs"], "forbidden_areas": ["secrets"],
                                **bounds}}}


class FakeEnv(ctl.WorkspaceEnvironment):
    def __init__(self):
        self.diff_text, self.sha, self.violation = "diff --git a b\n+x\n", "h1", None

    def head(self): return self.sha
    def diff(self): return self.diff_text
    def changed_files(self): return ["src/export/core.py"]
    def describe(self): return {"worktree": "W", "base_head": "base0"}

    def assert_safe(self):
        if self.violation:
            raise ac.GitPolicyViolation(self.violation)


def ok_checks(*names):
    return [{"name": n, "status": "PASS", "summary": "ok"} for n in (names or ("unit",))]


class Harness:
    """Scripted executors. A step is a dict or a `(ctx) -> dict` callable."""

    def __init__(self, tmp_path, *, mandate=None, env=None, roles=None):
        self.env, self.calls, self.scripts = env or FakeEnv(), [], {}
        self.stats = tmp_path / "stats"
        self.mandate = mandate or mandate_fixture()
        self.roles = roles or ac.load_roles(ROOT / "AUTONOMY_ROLES.json", ROOT / "IMPLEMENTER_PROFILES.json")
        # V0.1/V0.2 lifecycle tests keep exercising their historical role
        # bindings; V0.3 policy behavior has its own focused test module.
        self.roles.pop("policy_profiles", None)
        self.roles.pop("chain", None)   # lifecycle tests pin the classic per-iteration cycle; chain mode has its own tests
        self.ctxs = {}

    def script(self, name, *steps):
        self.scripts.setdefault(name, []).extend(steps)
        return self

    def _executor(self, name):
        def run(ctx):
            self.calls.append(name)
            self.ctxs.setdefault(name, []).append(ctx)
            queue = self.scripts.get(name)
            if not queue:
                raise AssertionError(f"unscripted {name} call (calls so far: {self.calls})")
            step = queue.pop(0) if len(queue) > 1 or name in ("plan",) else queue[0]
            return step(ctx) if callable(step) else step
        return run

    def executors(self, *, with_prep=False):
        names = list(ctl.EXECUTOR_NAMES) + (["prepare_packet"] if with_prep else [])
        return {n: self._executor(n) for n in names}

    def controller(self, run_id="RUN1", *, with_prep=False, resume=False):
        kwargs = dict(executors=self.executors(with_prep=with_prep), env=self.env, roles=self.roles,
                      stats_root=self.stats)
        if resume:
            return ctl.AutonomyController.resume(run_id, **kwargs)
        return ctl.AutonomyController.start(run_id, self.mandate, **kwargs)

    def defaults(self):
        """Happy-path scripts for every non-plan executor (sticky last step)."""
        self.script("execute", {"summary": "implemented", "checks": ok_checks()})
        self.script("self_verify", {"checks": ok_checks()})
        self.script("review", {"verdict": "PASS", "summary": "fine"})
        self.script("final_review", {"verdict": "PASS", "summary": "fine"})
        return self


def plan(refs, **over):
    def build(ctx):
        base = {"status": "ITERATION", "mandate_hash": ctx["mandate"]["mandate_hash"], "goal": f"do {refs}",
                "roadmap_refs": list(refs), "scope_justification": f"advances {refs} of the roadmap",
                "acceptance_criteria": (["export works", "tests added"] if ctx["iteration_index"] == 1
                                        else ["it works"]),
                "touched_areas": ["src/export"], "decisions": [{"kind": "REFACTORING"}], "skipped_items": []}
        base.update(over)
        return base
    return build


def end_plan(skipped=()):
    def build(ctx):
        return {"status": "NO_FURTHER_ACTION", "mandate_hash": ctx["mandate"]["mandate_hash"],
                "skipped_items": [{"item_id": i, "reason": "superseded by earlier work"} for i in skipped]}
    return build


def events(c, kind):
    return c.journal.by_type(kind)


# ── 1, 2: the core autonomy decision ─────────────────────────────────────────

def test_1_pass_with_roadmap_remaining_plans_the_next_iteration_without_a_human(tmp_path):
    h = Harness(tmp_path).defaults()
    h.script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    state = h.controller().run()
    assert [i["outcome"] for i in state["iterations"]] == ["PASS", "PASS", "PASS"]
    # Control never went to a human between iterations.
    holds = [e for e in h.controller("RUN1", resume=True).journal.read() if e["event_type"] == "AWAITING_HUMAN"]
    assert len(holds) == 1 and holds[0]["payload"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED
    decisions = [e["payload"] for e in ctl.AutonomyJournal(h.stats / "RUN1/AUTONOMY/autonomy_events.jsonl", "RUN1")
                 .by_type("ROADMAP_DECISION")]
    assert [d["next_action_available"] for d in decisions] == [True, True, False]


def test_2_pass_with_roadmap_exhausted_awaits_the_human(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = Harness(tmp_path, mandate=m).defaults().script("plan", plan(["A"]))
    state = h.controller().run()
    assert state["status"] == ac.AWAITING_HUMAN and state["phase"] == ac.AWAITING_HUMAN
    assert state["hold"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED and state["hold"]["roadmap_exhausted"]
    assert state["hold"]["promotable"] and state["escalation"] is None
    assert h.calls.count("plan") == 1  # no pointless extra planning round


def test_iteration_budget_is_a_hard_stop_for_the_human(tmp_path):
    h = Harness(tmp_path, mandate=mandate_fixture(max_iterations=1)).defaults().script("plan", plan(["A"]))
    state = h.controller().run()
    assert state["hold"]["reason"] == ac.HOLD_ITERATION_CAP and not state["hold"]["roadmap_exhausted"]


# ── 3, 4, 5: FINAL_REVIEW outcomes ───────────────────────────────────────────

def repair_flow(h, final_steps):
    h.defaults().script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["final_review"] = list(final_steps)
    h.script("repair", {"summary": "fixed", "checks": ok_checks()})
    return h


def test_3_and_4_repair_required_goes_to_repair_then_pass_resumes_roadmap_decision(tmp_path):
    h = repair_flow(Harness(tmp_path), [
        {"verdict": "REPAIR_REQUIRED", "findings": [{"finding_key": "F1", "severity": "HIGH", "summary": "edge case"}]},
        {"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}])
    c = h.controller()
    state = c.run()
    first = state["iterations"][0]
    assert h.calls[:10] == ["plan", "execute", "self_verify", "review", "final_review", "repair",
                           "self_verify", "review", "final_review", "plan"]
    assert first["repair_attempts"] == 1 and first["outcome"] == "PASS"
    assert first["repairs"][0]["addresses"] == ["F1"]
    assert [e["payload"]["verdict"] for e in events(c, "REVIEW_VERDICT")][:3] == ["PASS", "REPAIR_REQUIRED", "PASS"]
    assert state["iterations"][1]["index"] == 2  # roadmap decision -> next iteration


def test_review_findings_route_through_repair_to_final_review(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"], ), end_plan(["B", "C"]))
    h.scripts["review"] = [{"verdict": "REPAIR_REQUIRED",
                            "findings": [{"finding_key": "R1", "severity": "MEDIUM", "blocking": True, "summary": "x"}]}]
    h.script("repair", {"summary": "fixed R1", "checks": ok_checks()})
    state = h.controller().run()
    assert h.calls[:7] == ["plan", "execute", "self_verify", "review", "repair", "self_verify", "review"]
    assert state["escalation"]["code"] == ac.E_NO_PROGRESS


def test_5_final_review_escalate_stops_autonomy_and_is_not_promotable(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    h.scripts["final_review"] = [{"verdict": "ESCALATE", "summary": "product decision needed"}]
    state = h.controller().run()
    assert state["status"] == ac.AWAITING_HUMAN and state["escalation"]["code"] == ac.E_REVIEW
    assert not state["hold"]["promotable"] and state["iterations"][0]["outcome"] == "ESCALATE"
    with pytest.raises(ac.AutonomyError, match="not promotable"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id="x", stats_root=h.stats)


def test_malformed_review_result_fails_closed(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    h.scripts["review"] = [{"summary": "looks good to me"}]  # no verdict
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_REVIEW_INVALID


def test_pass_cannot_coexist_with_failing_evidence_or_blocking_findings(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    h.scripts["execute"] = [{"summary": "done", "checks": [{"name": "unit", "status": "FAIL"}]}]
    h.scripts["self_verify"] = [{"checks": ok_checks()}]  # self-verify claims green; review still sees stale FAIL?
    h.scripts["final_review"] = [{"verdict": "PASS", "findings": [
        {"finding_key": "B1", "severity": "CRITICAL", "summary": "data loss"}]}, {"verdict": "PASS"}]
    h.script("repair", {"summary": "fixed", "checks": ok_checks()})
    c = h.controller()
    c.run()
    verdicts = [e["payload"] for e in events(c, "REVIEW_VERDICT")]
    downgraded = [v for v in verdicts if v["downgraded_from"] == "PASS"]
    assert downgraded and downgraded[0]["verdict"] == "REPAIR_REQUIRED"


# ── 6, 12: scope creep and mandate protection ────────────────────────────────

@pytest.mark.parametrize("override,code", [
    ({"touched_areas": ["secrets/keys.txt"]}, ac.E_SCOPE),
    ({"touched_areas": ["infra/prod.tf"]}, ac.E_SCOPE),
    ({"roadmap_refs": ["NEW_ITEM"]}, ac.E_LINK),
    ({"roadmap_refs": []}, ac.E_LINK),
    ({"scope_justification": " "}, ac.E_LINK),
    ({"decisions": [{"kind": "PUBLIC_CONTRACT_CHANGE"}]}, ac.E_DECISION),
    ({"decisions": [{"kind": "SOMETHING_NEW"}]}, ac.E_DECISION),
    ({"acceptance_criteria": []}, ac.E_PLAN_INVALID),
])
def test_6_out_of_mandate_second_iteration_escalates(tmp_path, override, code):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), plan(["C"], **override))
    state = h.controller().run()
    assert state["status"] == ac.AWAITING_HUMAN and state["escalation"]["code"] == code
    assert len(state["iterations"]) == 1  # the offending iteration never started
    assert h.calls.count("execute") == 1


def test_6_a_dependency_on_unfinished_work_is_out_of_mandate(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["C"]), plan(["B"]))  # B depends on A, still pending
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_SCOPE and "depends on unfinished" in state["escalation"]["detail"]


def test_6_planner_requested_escalation_is_honoured(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), plan(["C"], status="ESCALATE", reason="conflict"))
    assert h.controller().run()["escalation"]["code"] == ac.E_PLANNER


def test_12_planner_cannot_extend_its_own_mandate(tmp_path):
    h = Harness(tmp_path).defaults().script(
        "plan", plan(["A"]), plan(["C"], new_roadmap_items=[{"item_id": "Z", "title": "more"}]))
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_EXTENSION
    assert "Z" not in state["roadmap"]


def test_12_plan_derived_from_a_different_mandate_is_refused(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), plan(["C"], mandate_hash="sha256:forged"))
    assert h.controller().run()["escalation"]["code"] == ac.E_MANDATE_MISMATCH


def test_12_tampering_with_the_persisted_mandate_is_detected_on_resume(tmp_path):
    h = Harness(tmp_path, mandate=mandate_fixture(max_iterations=1)).defaults().script("plan", plan(["A"]))
    h.controller().run()
    path = h.stats / "RUN1/AUTONOMY/autonomy_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["mandate"]["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] = 50
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ac.AutonomyError, match=ac.E_MANDATE_TAMPERED):
        ctl.load_state("RUN1", h.stats)


def test_iteration_one_cannot_weaken_the_humans_acceptance_criteria(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"], acceptance_criteria=["export works"]))
    assert h.controller().run()["escalation"]["code"] == ac.E_SCOPE


# ── 11: roadmap is direction, not a backlog ─────────────────────────────────

def test_11_planner_may_justifiably_skip_and_stop_early(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    state = h.controller().run()
    assert state["hold"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED and state["hold"]["promotable"]
    assert {k: v["status"] for k, v in state["roadmap"].items()} == {"A": "DONE", "B": "SKIPPED", "C": "SKIPPED"}
    assert state["roadmap"]["B"]["reason"] == "superseded by earlier work"


def test_11_planner_may_skip_an_item_and_still_run_another(tmp_path):
    h = Harness(tmp_path).defaults().script(
        "plan", plan(["A"]), plan(["C"], skipped_items=[{"item_id": "B", "reason": "csv covered by A"}]), end_plan())
    state = h.controller().run()
    assert state["roadmap"]["B"]["status"] == "SKIPPED" and state["roadmap"]["C"]["status"] == "DONE"


def test_11_silently_dropping_the_roadmap_is_an_escalation(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B"]))  # C unexplained
    assert h.controller().run()["escalation"]["code"] == ac.E_UNEXPLAINED_END


def test_11_skip_without_reason_is_an_escalation(tmp_path):
    h = Harness(tmp_path).defaults().script(
        "plan", plan(["A"]), plan(["C"], skipped_items=[{"item_id": "B", "reason": ""}]))
    assert h.controller().run()["escalation"]["code"] == ac.E_LINK


# ── 10: repair loop is bounded ───────────────────────────────────────────────

def test_10_repair_limit_escalates_instead_of_looping(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    counter = iter(range(100))
    h.scripts["final_review"] = [lambda ctx: {"verdict": "REPAIR_REQUIRED", "findings": [
        {"finding_key": f"K{next(counter)}", "severity": "HIGH", "summary": "still wrong"}]}]
    h.script("repair", {"summary": "tried", "checks": ok_checks()})
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_REPAIR_LIMIT
    assert h.calls.count("repair") == 2 == state["iterations"][0]["repair_attempts"]  # == max_repair_attempts
    assert not state["hold"]["promotable"]


def test_10_identical_findings_climb_the_repair_ladder_before_stopping_as_no_progress(tmp_path):
    # A single repair that leaves the same finding no longer stops the run: the ladder
    # (CURRENT -> EFFORT_UP -> DIFFICULT_IMPLEMENTER) is tried first; only its exhaustion is a Human Gate.
    h = Harness(tmp_path, mandate=mandate_fixture(max_repair_attempts=5)).defaults().script("plan", plan(["A"]))
    h.scripts["final_review"] = [{"verdict": "REPAIR_REQUIRED", "findings": [
        {"finding_key": "SAME", "severity": "HIGH", "summary": "x"}]}]
    h.script("repair", {"summary": "tried", "checks": ok_checks()})
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_NO_PROGRESS and h.calls.count("repair") == 3
    stages = [r["stage"] for r in state["iterations"][0]["repairs"]]
    assert stages == ["CURRENT", "EFFORT_UP", "DIFFICULT_IMPLEMENTER"]
    assert "exhausted" in state["escalation"]["detail"]


def test_mandate_cannot_raise_the_hard_ceilings():
    for key, value in (("max_iterations", 10_000), ("max_repair_attempts", 99), ("max_iterations", 0)):
        m = mandate_fixture()
        m["roadmap_mandate"]["autonomy_bounds"][key] = value
        with pytest.raises(ac.AutonomyError):
            ac.validate_mandate(m)


def test_self_verify_failure_repairs_then_reverifies_before_review(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    h.scripts["self_verify"] = [{"checks": [{"name": "lint", "status": "FAIL"}]}, {"checks": [{"name": "lint", "status": "PASS"}]}]
    h.script("repair", {"summary": "lint fixed", "checks": [{"name": "lint", "status": "PASS"}]})
    state = h.controller().run()
    assert h.calls[:7] == ["plan", "execute", "self_verify", "repair", "self_verify", "review", "final_review"]
    assert state["iterations"][0]["outcome"] == "PASS"


# ── 7: promotion needs a human ───────────────────────────────────────────────

def held_run(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = Harness(tmp_path, mandate=m).defaults().script("plan", plan(["A"]))
    state = h.controller().run()
    return h, state["hold"]["candidate_id"]


def test_7_promote_is_refused_without_human_approval(tmp_path):
    h, _ = held_run(tmp_path)
    with pytest.raises(ac.AutonomyError, match="requires a recorded human approval"):
        ctl.promote("RUN1", stats_root=h.stats)
    assert ctl.load_state("RUN1", h.stats)["status"] == ac.AWAITING_HUMAN


def test_7_the_state_machine_only_reaches_promote_through_human_approval():
    def reach(without):
        seen, stack = set(), [ac.PLAN]
        while stack:
            node = stack.pop()
            if node in seen or node == without:
                continue
            seen.add(node)
            stack.extend(ac.TRANSITIONS[node])
        return seen
    assert ac.PROMOTE in reach(without=None)
    assert ac.PROMOTE not in reach(without=ac.HUMAN_APPROVED)
    assert ac.HUMAN_APPROVED not in reach(without=ac.AWAITING_HUMAN)
    assert [p for p, targets in ac.TRANSITIONS.items() if ac.PROMOTE in targets] == [ac.HUMAN_APPROVED]
    assert [p for p, targets in ac.TRANSITIONS.items() if ac.HUMAN_APPROVED in targets] == [ac.AWAITING_HUMAN]


def test_7_agents_cannot_approve_and_the_wrong_candidate_cannot_be_approved(tmp_path):
    h, cand = held_run(tmp_path)
    for who in ("planner", "OPUS_5_5_HIGH", "gpt-5.6-sol", "implementer"):
        with pytest.raises(ac.AutonomyError, match="human identity"):
            ctl.approve_promotion("RUN1", approver=who, candidate_id=cand, stats_root=h.stats)
    with pytest.raises(ac.AutonomyError, match="HUMAN channel"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, channel="AGENT", stats_root=h.stats)
    with pytest.raises(ac.AutonomyError, match="candidate_id"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id="CAND_other", stats_root=h.stats)
    assert ctl.load_state("RUN1", h.stats)["status"] == ac.AWAITING_HUMAN


def test_7_full_path_approve_then_promote_records_without_merging(tmp_path):
    h, cand = held_run(tmp_path)
    ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, stats_root=h.stats)
    assert h.controller(resume=True).run()["status"] == ac.HUMAN_APPROVED  # a restart cannot walk past the gate
    state = ctl.promote("RUN1", stats_root=h.stats)
    assert state["status"] == ac.PROMOTED
    assert state["promotion"]["integration"] == {"status": "READY_FOR_EXTERNAL_INTEGRATION", "merged": False,
                                                 "pushed": False}
    with pytest.raises(ac.AutonomyError):  # one approval, one promotion
        ctl.promote("RUN1", stats_root=h.stats)


def test_7_early_end_requires_an_explicit_human_decision(tmp_path):
    h = Harness(tmp_path, mandate=mandate_fixture(max_iterations=1)).defaults().script("plan", plan(["A"]))
    cand = h.controller().run()["hold"]["candidate_id"]
    with pytest.raises(ac.AutonomyError, match="early_end"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, stats_root=h.stats)
    state = ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, early_end=True, stats_root=h.stats)
    assert state["human"]["early_end"] is True


def test_reject_closes_the_run_and_blocks_promotion(tmp_path):
    h, _ = held_run(tmp_path)
    ctl.reject("RUN1", approver="kamil", reason="not what I meant", stats_root=h.stats)
    with pytest.raises(ac.AutonomyError):
        ctl.promote("RUN1", stats_root=h.stats)


def test_a_running_run_cannot_be_approved(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    c = h.controller()  # started, not run
    # V0.2: while a controller owns the run, the human surface is refused outright.
    with pytest.raises(ac.AutonomyError, match="RUN_LOCK_BUSY"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id="x", stats_root=h.stats)
    c.release()
    with pytest.raises(ac.AutonomyError, match="only possible in AWAITING_HUMAN"):
        ctl.approve_promotion("RUN1", approver="kamil", candidate_id="x", stats_root=h.stats)


# ── 8, 9: git boundaries ─────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["git", "merge", "aaw/slice"], ["git", "-C", "repo", "merge", "--no-ff", "feature"],
    ["git", "pull"], ["git", "rebase", "main"], ["git", "cherry-pick", "abc"],
    ["git", "checkout", "main"], ["git", "switch", "master"], ["git", "update-ref", "refs/heads/main", "abc"],
    ["git", "branch", "-f", "main", "HEAD"], ["git", "reset", "--hard", "main"],
])
def test_8_agent_git_cannot_integrate_into_main(argv):
    ok, why = ac.classify_git_command(argv)
    assert not ok, why
    sentinel = []
    guarded = ac.GuardedGit(lambda a: sentinel.append(a) or (0, "", ""))
    with pytest.raises(ac.GitPolicyViolation):
        guarded.run(argv)
    assert sentinel == []  # denied before anything ran


@pytest.mark.parametrize("argv", [["git", "commit", "-m", "x"], ["git", "status"], ["git", "diff", "HEAD"],
                                  ["git", "checkout", "-b", "aaw/new"], ["git", "add", "."]])
def test_local_checkpoint_commands_stay_allowed(argv):
    assert ac.classify_git_command(argv)[0]


def test_9_push_is_denied_without_the_promotion_gate_and_one_shot_with_it():
    ran = []
    runner = lambda a: ran.append(list(a)) or (0, "", "")
    with pytest.raises(ac.GitPolicyViolation):
        ac.GuardedGit(runner).run(["git", "push", "origin", "main"])
    denied = []
    token = ac.PromotionToken("RUN1", "APR_x")
    guarded = ac.GuardedGit(runner, token=token, on_denied=lambda a, w: denied.append(a))
    guarded.run(["git", "push", "origin", "main"])
    assert ran == [["git", "push", "origin", "main"]]
    with pytest.raises(ac.GitPolicyViolation):  # the token is spent
        guarded.run(["git", "push", "origin", "main"])
    assert len(denied) == 1


def test_promoter_hook_receives_a_token_only_after_approval(tmp_path):
    h, cand = held_run(tmp_path)
    seen = []
    with pytest.raises(ac.AutonomyError):
        ctl.promote("RUN1", promoter=lambda t, s: seen.append(t) or {}, stats_root=h.stats)
    assert seen == []
    ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, stats_root=h.stats)
    # V0.2: an integrating promoter re-verifies the approved candidate against the workspace.
    state = ctl.promote("RUN1", promoter=lambda t, s: (seen.append(t), {"status": "INTEGRATED"})[1], stats_root=h.stats,
                        env=h.env)
    assert len(seen) == 1 and seen[0].candidate_id == cand and seen[0].valid() and state["promotion"]["integration"]["status"] == "INTEGRATED"


def test_a_git_boundary_violation_between_phases_escalates(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    h.scripts["execute"] = [lambda ctx: setattr(ctx["env"], "violation", "protected branch main moved") or
                            {"summary": "merged to main?!", "checks": ok_checks()}]
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_GIT and h.calls.count("review") == 0


def real_workspace(root):
    root.mkdir(parents=True, exist_ok=True)
    repo, worktree = root / "repo", root / "wt"
    repo.mkdir()
    run = lambda argv, cwd: subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.email", "t@example.invalid"], repo)
    run(["git", "config", "user.name", "T"], repo)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "base"], repo)
    run(["git", "worktree", "add", "-b", "aaw/it", str(worktree)], repo)
    return repo, worktree, run


def test_8_real_git_a_merge_into_main_is_detected_at_the_next_boundary(tmp_path):
    repo, worktree, run = real_workspace(tmp_path / "ws")
    env = ctl.GitWorkspaceEnvironment(repo, worktree)
    env.assert_safe()
    (worktree / "b.txt").write_text("y\n", encoding="utf-8")
    run(["git", "add", "."], worktree)
    run(["git", "commit", "-m", "iteration checkpoint"], worktree)
    env.assert_safe()  # a local commit on the iteration branch is fine
    # V0.2 defect fix: V0.1 diffed against HEAD, so a checkpoint commit made the
    # iteration's work invisible to the reviewer. The diff is now base..worktree.
    assert "+++ b/b.txt" in env.diff() and "b.txt" in env.changed_files()
    assert [c.split(" ", 1)[1] for c in env.commits()] == ["iteration checkpoint"]
    run(["git", "merge", "--ff-only", "aaw/it"], repo)  # what an agent must never do
    with pytest.raises(ac.GitPolicyViolation):
        env.assert_safe()


def test_8_real_git_refuses_to_run_in_the_canonical_checkout(tmp_path):
    repo, _, _ = real_workspace(tmp_path / "ws")
    with pytest.raises(ac.GitPolicyViolation):
        ctl.GitWorkspaceEnvironment(repo, repo)


# ── 13: lineage and audit ────────────────────────────────────────────────────

def test_13_every_iteration_traces_back_to_the_frozen_mandate(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), plan(["B", "C"]), end_plan())
    c = h.controller()
    state = c.run()
    first, second = state["iterations"]
    for it in state["iterations"]:
        assert it["lineage"]["mandate_id"] == "M1" and it["lineage"]["mandate_hash"] == state["mandate_hash"]
        assert it["lineage"]["scope_justification"] and it["plan"]["acceptance_criteria"]
        assert it["planned_by"]["role"] == "planner" and it["executed_by"]["role"] == "implementer"
    assert first["lineage"]["source"] == "ITERATION_CONTRACT" and first["lineage"]["parent_iteration_id"] is None
    assert second["lineage"]["source"] == "ROADMAP_MANDATE"
    assert second["lineage"]["roadmap_refs"] == ["B", "C"]
    assert second["lineage"]["parent_iteration_id"] == first["iteration_id"]
    planned = events(c, "ITERATION_PLANNED")
    assert [e["payload"]["lineage"]["mandate_hash"] for e in planned] == [state["mandate_hash"]] * 2


def test_audit_answers_who_what_why_and_when_it_stopped(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), plan(["B"], decisions=[{"kind": "GOAL_CHANGE"}]))
    c = h.controller()
    c.run()
    kinds = [e["event_type"] for e in c.journal.read()]
    assert kinds[0] == "MANDATE_FROZEN" and kinds.index("ITERATION_PLANNED") < kinds.index("ITERATION_ACCEPTED")
    assert "ROADMAP_DECISION" in kinds and "SCOPE_CHECK" in kinds
    esc = events(c, "ESCALATED")[0]["payload"]
    assert esc["code"] == ac.E_DECISION
    frozen = events(c, "MANDATE_FROZEN")[0]["payload"]["roles"]
    assert frozen["planner"]["profile_id"] == "OPUS_5_5_HIGH" and frozen["reviewer"]["review_independence"] == "DIFFERENT_MODEL"
    seqs = [e["sequence"] for e in c.journal.read()]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))


# ── 14: the packet is a map, not the territory ──────────────────────────────

def test_14_reviewer_receives_the_real_diff_alongside_the_packet(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    h.controller().run()
    ctx = h.ctxs["review"][0]
    assert "diff" not in ctx["raw"] and ctx["raw"]["head"] == "h1"
    assert Path(ctx["raw"]["diff_path"]).read_text(encoding="utf-8") == h.env.diff_text
    assert ctx["packet"]["authoritative"] is False and ctx["packet"]["access"]["source_of_truth"] == "REPOSITORY_STATE"
    assert ctx["packet"]["access"]["diff_sha256"] == ctx["raw"]["diff_sha256"]
    assert {"TASK", "PLAN", "IMPLEMENTATION", "EVIDENCE", "RISKS", "REVIEW_TARGETS"} <= set(ctx["packet"])
    assert ctx["raw"]["evidence"] and ctx["binding"]["profile_id"] == "SOL_6_1_LIGHT"


def test_14_a_stale_packet_is_rebuilt_not_trusted(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    c = h.controller()
    original = c._build_packet

    def build_then_drift(kind):
        packet = original(kind)
        if kind == "REVIEW" and not events(c, "PACKET_STALE"):
            h.env.diff_text += "+late change\n"
        return packet
    c._build_packet = build_then_drift
    c.run()
    assert events(c, "PACKET_STALE")
    ctx = h.ctxs["review"][0]
    assert ctx["packet"]["access"]["diff_sha256"] == ctx["raw"]["diff_sha256"]


def test_14_compression_cannot_drop_failures_warnings_or_deviations(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    h.scripts["execute"] = [{"summary": "done", "deviations": ["swapped library X for Y"],
                             "uncertainties": ["thread safety unproven"],
                             "checks": [{"name": "unit", "status": "PASS", "warnings": ["DeprecationWarning: foo"]},
                                        {"name": "typecheck", "status": "SKIPPED", "summary": "no mypy here"}]}]
    h.scripts["self_verify"] = [{"checks": [{"name": "lint", "status": "WARN", "summary": "3 warnings"}]}]

    def optimistic_compressor(ctx):  # a cheap model that "forgets" everything unflattering
        packet = copy.deepcopy(ctx["packet"])
        packet["RISKS"] = {"adverse_items": [], "uncertainties": [], "unresolved": []}
        packet["IMPLEMENTATION"]["deviations_from_plan"] = []
        packet["authoritative"] = True
        packet["access"]["diff_sha256"] = "sha256:lies"
        return packet
    h.script("prepare_packet", optimistic_compressor)
    c = h.controller(with_prep=True)
    c.run()
    packet = h.ctxs["review"][0]["packet"]
    texts = json.dumps(packet["RISKS"]["adverse_items"])
    for must_survive in ("DeprecationWarning: foo", "swapped library X for Y", "thread safety unproven",
                         "no mypy here", "3 warnings"):
        assert must_survive in texts
    assert packet["authoritative"] is False
    assert any(e["event_type"] == "REVIEW_PRETREATMENT_REJECTED" for e in c.journal.read())
    assert packet["access"]["diff_sha256"] == h.ctxs["review"][0]["raw"]["diff_sha256"]
    assert h.ctxs["prepare_packet"][0]["binding"]["profile_id"] == "GPT6_LUNA_VERY_HIGH"


def test_14_failures_survive_into_the_final_review_packet_after_repair(tmp_path):
    h = repair_flow(Harness(tmp_path), [
        {"verdict": "REPAIR_REQUIRED", "findings": [{"finding_key": "F1", "severity": "HIGH", "summary": "edge case"}]},
        {"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}])
    h.controller().run()
    final_packet = h.ctxs["final_review"][1]["packet"]
    assert final_packet["packet_kind"] == "FINAL_REVIEW"
    assert any(a.get("finding_key") == "F1" for a in final_packet["RISKS"]["adverse_items"])
    assert final_packet["IMPLEMENTATION"]["repairs"][0]["summary"] == "fixed"


# ── 15: restart / resume ────────────────────────────────────────────────────

class Crash(BaseException):
    """Process death: not an Exception, so nothing in the controller can swallow it."""


def test_15_resume_after_a_crash_in_a_read_only_phase_loses_nothing(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    boom = {"armed": True}

    def crashing_review(ctx):
        if boom.pop("armed", False):
            raise Crash()
        return {"verdict": "PASS"}
    h.scripts["review"] = [crashing_review]
    c = h.controller()
    with pytest.raises(Crash):
        c.run()
    persisted = ctl.load_state("RUN1", h.stats)
    assert persisted["phase"] == ac.REVIEW and persisted["in_flight"]["phase"] == ac.REVIEW
    assert persisted["iterations"][0]["execution"]["summary"] == "implemented"

    calls_before = list(h.calls)
    state = h.controller(resume=True).run()
    # Nothing that had already completed ran again; the read-only phase did.
    assert h.calls[len(calls_before):] == ["review", "final_review", "plan"]
    assert state["iterations"][0]["outcome"] == "PASS" and state["hold"]["promotable"]
    assert state["iterations"][0]["lineage"]["mandate_hash"] == state["mandate_hash"]
    assert events(h.controller(resume=True), "RUN_RESUMED")


def test_15_resume_never_repeats_a_side_effecting_phase(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    h.scripts["execute"] = [lambda ctx: (_ for _ in ()).throw(Crash())]
    with pytest.raises(Crash):
        h.controller().run()
    state = h.controller(resume=True).run()
    assert state["escalation"]["code"] == ac.E_INTERRUPTED and state["status"] == ac.AWAITING_HUMAN
    assert h.calls.count("execute") == 1 and not state["hold"]["promotable"]


def test_15_resume_of_a_held_run_keeps_the_hold_and_the_candidate(tmp_path):
    h, cand = held_run(tmp_path)
    state = h.controller(resume=True).run()
    assert state["hold"]["candidate_id"] == cand and state["status"] == ac.AWAITING_HUMAN
    assert h.calls.count("plan") == 1


def test_15_resume_preserves_repair_counters_so_the_limit_cannot_be_reset(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    n = iter(range(100))
    h.scripts["final_review"] = [lambda ctx: {"verdict": "REPAIR_REQUIRED", "findings": [
        {"finding_key": f"K{next(n)}", "severity": "HIGH", "summary": "x"}]}]
    crash = {"on": 2}

    def repair(ctx):
        crash["on"] -= 1
        if crash["on"] == 0:
            raise Crash()
        return {"summary": "tried", "checks": ok_checks()}
    h.scripts["repair"] = [repair]
    with pytest.raises(Crash):
        h.controller().run()
    assert ctl.load_state("RUN1", h.stats)["iterations"][0]["repair_attempts"] == 2
    state = h.controller(resume=True).run()  # in-flight REPAIR -> human, not a fresh budget
    assert state["escalation"]["code"] == ac.E_INTERRUPTED


def test_state_is_written_atomically_at_every_boundary(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]), end_plan(["B", "C"]))
    c = h.controller()
    seen = []
    original = c._goto

    def spy(phase):
        original(phase)
        seen.append(phase)
    c._goto = spy
    c.run()
    assert seen[:6] == [ac.EXECUTE, ac.SELF_VERIFY, ac.AWAITING_REVIEW, ac.REVIEW, ac.FINAL_REVIEW, ac.ROADMAP_CHECK]
    assert json.loads((h.stats / "RUN1/AUTONOMY/autonomy_state.json").read_text(encoding="utf-8"))["status"] == ac.AWAITING_HUMAN


# ── roles and misc ──────────────────────────────────────────────────────────

def test_executor_failure_is_an_escalation_not_a_crash(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    h.scripts["execute"] = [lambda ctx: (_ for _ in ()).throw(ctl.ExecutorFailure("harness unavailable"))]
    state = h.controller().run()
    assert state["escalation"]["code"] == ac.E_EXECUTOR and "harness unavailable" in state["escalation"]["detail"]


def test_roles_are_configuration_not_logic(tmp_path):
    profiles = json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text(encoding="utf-8"))
    catalog = {p["profile_id"]: p for p in profiles["profiles"]}
    cfg = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    cfg["roles"]["implementer"] = {"profile_id": "SONNET_HIGH"}  # re-bind without touching code
    assert ac.validate_roles(cfg, catalog)["implementer"]["runtime_model_id"] == "claude-sonnet-5"
    cfg["roles"]["reviewer"] = cfg["roles"]["implementer"]
    with pytest.raises(ac.AutonomyError, match="allow_same_model_fresh_context"):
        ac.validate_roles(cfg, catalog)
    cfg["allow_same_model_fresh_context"] = True
    assert ac.validate_roles(cfg, catalog)["reviewer"]["review_independence"] == "SAME_MODEL_FRESH_CONTEXT"
    cfg["roles"]["planner"] = {"profile_id": "NOPE"}
    with pytest.raises(ac.AutonomyError, match="unknown profile"):
        ac.validate_roles(cfg, catalog)
    for module in ("autonomy_contract.py", "autonomy_controller.py"):
        source = (ROOT / module).read_text(encoding="utf-8")
        assert "gpt-" not in source and "claude-" not in source and "Luna" not in source


def test_start_refuses_to_overwrite_an_existing_run(tmp_path):
    h = Harness(tmp_path).defaults().script("plan", plan(["A"]))
    h.controller()
    with pytest.raises(ac.AutonomyError, match="already exists"):
        h.controller()


def test_cli_refuses_agent_style_approval(tmp_path, monkeypatch, capsys):
    h, cand = held_run(tmp_path)
    monkeypatch.setattr(ctl, "STATS_ROOT", h.stats)
    rc = ctl.main(["--approve", "RUN1", "--approver", "planner", "--candidate-id", cand])
    assert rc == 1 and "human identity" in capsys.readouterr().err
