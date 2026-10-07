"""Bounded repair escalation + quota routing, driven through the real controller.

The scripted executors stand in for the provider boundary (same convention as
test_autonomy*.py); the controller, router, ladder, ledger and journal are real.
"""

import json
from pathlib import Path

import pytest

import autonomy_adapters as aa
import autonomy_contract as ac
import autonomy_controller as ctl
import repair_escalation as rx
from test_autonomy import FakeEnv, Harness, mandate_fixture, ok_checks
from test_autonomy_policy_v0_3 import initial_plan

ROOT = Path(__file__).parent
HIGH, VHIGH, MAX = "GPT6_LUNA_HIGH", "GPT6_LUNA_VERY_HIGH", "GPT6_LUNA_MAX"
SONNET, OPUS = "SONNET_5_5_MEDIUM", "OPUS_5_5_HIGH"
AC8 = "AC8 — required post-install checks"


def roles_with(*, repair_default=None, routing=None, escalation=None, drop_routing=False):
    config = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    config.pop("chain", None)   # these tests pin the classic per-iteration cycle
    profiles = {p["profile_id"]: p for p in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
    if repair_default:
        config["policy_profiles"]["repair_default"] = repair_default
    if routing is not None:
        config["routing"] = routing
    if drop_routing:
        config.pop("routing")
    if escalation is not None:
        config["repair_escalation"] = escalation
    return ac.validate_roles(config, profiles)


class MutableEnv(FakeEnv):
    """FakeEnv whose diff/changed files a repair script can move."""

    def __init__(self):
        super().__init__()
        self.files = ["src/export/core.py"]

    def changed_files(self):
        return list(self.files)


def one_item(**bounds):
    m = mandate_fixture(**bounds)
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only item"}]
    return m


class Rig:
    def __init__(self, tmp_path, *, roles=None, mandate=None, diagnose=True, quota=None, clock=None):
        self.env = MutableEnv()
        self.h = Harness(tmp_path, mandate=mandate or one_item(max_repair_attempts=2), env=self.env)
        self.h.roles = roles or roles_with()
        self.h.defaults().script("plan", initial_plan(["A"]))
        self.use_diagnose, self.quota, self.clock = diagnose, quota, clock
        self.tmp = tmp_path

    def script(self, name, *steps):
        self.h.script(name, *steps)
        return self

    def replace(self, name, *steps):
        self.h.scripts[name] = list(steps)
        return self

    def controller(self):
        executors = self.h.executors()
        if self.use_diagnose:
            if "diagnose" not in self.h.scripts:
                counter = iter(range(1000))
                self.h.script("diagnose", lambda ctx: {"summary": "diagnosed", "diagnosis": {
                    "root_cause": f"hypothesis {next(counter)}"}})
            executors["diagnose"] = self.h._executor("diagnose")
        return ctl.AutonomyController.start("RUN1", self.h.mandate, executors=executors, env=self.env,
                                            roles=self.h.roles, stats_root=self.h.stats,
                                            quota_source=self.quota, clock=self.clock)

    @property
    def calls(self):
        return self.h.calls

    def ledger(self):
        path = self.h.stats / "RUN1" / "AUTONOMY" / "repair_escalation_ledger.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def journal(self, kind):
        return ctl.AutonomyJournal(self.h.stats / "RUN1/AUTONOMY/autonomy_events.jsonl", "RUN1").by_type(kind)


FINDING = {"verdict": "REPAIR_REQUIRED", "findings": [
    {"finding_key": "F1", "severity": "HIGH", "summary": "edge case unhandled", "evidence_ref": "RAW.diff"}]}


def stuck(rig):
    """Primary review keeps reporting the same finding after every repair."""
    rig.h.scripts["review"] = [dict(FINDING)]
    rig.script("repair", {"summary": "tried", "checks": ok_checks(), "changed_files": []})
    return rig


def repair_rows(state):
    return state["iterations"][0]["repairs"]


# ── 1. the finding disappears after REPAIR → no escalation ──────────────────

def test_1_finding_resolved_by_repair_needs_no_escalation(tmp_path):
    rig = Rig(tmp_path)
    rig.h.scripts["review"] = [dict(FINDING), {"verdict": "PASS"}]
    rig.script("repair", {"summary": "handled the edge case", "checks": ok_checks()})
    state = rig.controller().run()
    it = state["iterations"][0]
    assert it["outcome"] == "PASS" and it["repair_attempts"] == 1 and not it.get("repair_escalations")
    assert rig.ledger() == [] and state["escalation"] is None


# ── 2-4. the ladder: effort up, then more effort, then the difficult implementer ─

def test_2_surviving_finding_climbs_from_high_to_very_high(tmp_path):
    rig = stuck(Rig(tmp_path, roles=roles_with(repair_default=HIGH)))
    state = rig.controller().run()
    rows = repair_rows(state)
    assert [(r["stage"], r["profile_id"]) for r in rows[:2]] == [("CURRENT", HIGH), ("EFFORT_UP", VHIGH)]
    assert rows[1]["mode"] == rx.MODE_DIAGNOSE_THEN_REPAIR
    entry = next(e for e in rig.ledger() if e["entry_type"] == "ESCALATION")
    assert entry["previous_model_effort"]["profile_id"] == HIGH and entry["new_model_effort"]["profile_id"] == VHIGH
    assert entry["new_model_effort"]["effort"] == "xhigh" and entry["finding_ids"] == ["F1"]


def test_3_very_high_remaining_climbs_to_max(tmp_path):
    rig = stuck(Rig(tmp_path))             # default repair profile is very-high
    state = rig.controller().run()
    assert [(r["stage"], r["profile_id"]) for r in repair_rows(state)[:2]] == [("CURRENT", VHIGH), ("EFFORT_UP", MAX)]


def test_4_max_remaining_escalates_to_the_difficult_implementer_with_diagnosis_first(tmp_path):
    rig = stuck(Rig(tmp_path, roles=roles_with(repair_default=MAX)))
    rig.script("diagnose", {"summary": "d", "diagnosis": {"root_cause": "off-by-one in the export window"}})
    state = rig.controller().run()
    rows = repair_rows(state)
    assert [(r["stage"], r["profile_id"]) for r in rows[:2]] == [("CURRENT", MAX), ("DIFFICULT_IMPLEMENTER", SONNET)]
    calls = rig.calls
    first_diag = calls.index("diagnose")
    assert calls[first_diag - 1] == "review" and calls[first_diag + 1] == "repair"   # DIAGNOSE, then REPAIR
    diag_exec = next(e for e in state["executions"] if e["executor"] == "diagnose")
    assert diag_exec["profile"] == SONNET
    assert state["iterations"][0]["diagnoses"][0]["root_cause"].startswith("off-by-one")


# ── 5. no code diff, but new test evidence removes the finding → PASS ───────

def failing_ac8_execute():
    return {"summary": "implemented", "changed_files": ["src/export/core.py"], "checks": [
        *ok_checks(), {"name": AC8, "status": "FAIL", "summary": "pytest tests/post_install -> exit 1, 3 failed",
                       "command": "pytest tests/post_install", "exit_code": 1}]}


def test_5_new_test_result_without_a_code_diff_closes_a_self_verify_finding(tmp_path):
    rig = Rig(tmp_path)
    rig.replace("execute", failing_ac8_execute())
    rig.script("repair", {"summary": "re-ran the post-install checks (nothing to change in the product)",
                          "changed_files": [], "checks": [{"name": AC8, "status": "PASS", "exit_code": 0,
                                                            "summary": "pytest tests/post_install -> exit 0, 12 passed"}]})
    state = rig.controller().run()
    it = state["iterations"][0]
    assert it["outcome"] == "PASS" and state["escalation"] is None
    repair = it["repairs"][0]
    assert repair["addresses"] == [f"SELF_VERIFY::{AC8}"]
    assert repair["code_changed"] is False and repair["evidence_changed"] is True
    assert "NEW_TEST_RESULT" in repair["signals"] and not it.get("repair_escalations")


def test_5b_a_rerun_under_a_new_name_supersedes_the_stale_failing_check(tmp_path):
    rig = Rig(tmp_path)
    rig.replace("execute", failing_ac8_execute())
    rig.script("repair", {"summary": "ran the full post-install suite", "changed_files": [], "checks": [
        {"name": "post-install suite", "status": "PASS", "exit_code": 0, "log_ref": "LOG:post-install-2",
         "summary": "exit 0, 12 passed", "supersedes": [AC8]}]})
    state = rig.controller().run()
    it = state["iterations"][0]
    assert it["outcome"] == "PASS" and it["evidence_state"][AC8] == "SUPERSEDED"
    assert "STALE_EVIDENCE_SUPERSEDED" in it["repairs"][0]["signals"]
    assert any(c["status"] == "SUPERSEDED" and c.get("log_ref") for c in it["checks"])   # kept visible, not erased


# ── 6. stale review finding disappears on re-review without touching code ───

def test_6_stale_finding_is_dropped_by_re_review_without_an_artificial_diff(tmp_path):
    rig = Rig(tmp_path)
    rig.h.scripts["review"] = [dict(FINDING), {"verdict": "PASS"}]
    rig.script("repair", {"summary": "finding is stale: the guard already exists", "changed_files": [], "checks": [],
                          "diagnosis": {"root_cause": "reviewer read an outdated revision", "classification": "STALE_FINDING"}})
    before = rig.env.diff_text
    state = rig.controller().run()
    it = state["iterations"][0]
    assert it["outcome"] == "PASS" and rig.env.diff_text == before
    assert it["repairs"][0]["code_changed"] is False and "BETTER_DIAGNOSIS" in it["repairs"][0]["signals"]
    assert state["escalation"] is None and rig.ledger() == []


# ── 7. pre-existing / environmental limitations, classified with evidence ───

@pytest.mark.parametrize("classification,status", [("PRE_EXISTING_BASELINE", "BASELINE_FAILURE"),
                                                   ("ENVIRONMENTAL_LIMITATION", "ENVIRONMENTAL_LIMITATION")])
def test_7_evidence_backed_classification_does_not_block_pass_and_stays_visible(tmp_path, classification, status):
    rig = Rig(tmp_path)
    rig.replace("execute", {"summary": "ok", "changed_files": [], "checks": [
        *ok_checks(), {"name": "markdown format", "status": "FAIL", "summary": "3 files fail the formatter"}]})
    rig.script("repair", {"summary": "proved the failure exists on the base commit", "changed_files": [], "checks": [],
                          "reclassifications": [{"check_name": "markdown format", "classification": classification,
                                                 "evidence_ref": "BASELINE:git-stash-run#1", "acceptance_impact": "NONE",
                                                 "explanation": "reproduced on the untouched base commit"}]})
    c = rig.controller()
    state = c.run()
    it = state["iterations"][0]
    assert it["outcome"] == "PASS" and it["evidence_state"]["markdown format"] == status
    assert it["classified"][0]["evidence_ref"] == "BASELINE:git-stash-run#1"
    accepted = c.journal.by_type("ITERATION_ACCEPTED")[0]["payload"]
    assert accepted["classified_limitations"][0]["classification"] == classification
    assert any(a["source"] == "CHECK" and a["status"] == status for a in
               ac.adverse_items(execution=it["execution"], checks=it["checks"]))   # reviewers still see it
    assert {"EVIDENCE_BACKED_RECLASSIFICATION"} <= set(it["repairs"][0]["signals"])


def test_7b_a_classification_without_evidence_or_touching_an_acceptance_criterion_is_rejected(tmp_path):
    rig = Rig(tmp_path, roles=roles_with(escalation={"enabled": True, "stages": ["CURRENT"]}))
    bad = [{"check_name": "markdown format", "classification": "PRE_EXISTING_BASELINE", "evidence_ref": "",
            "acceptance_impact": "NONE", "explanation": "x"},
           {"check_name": "markdown format", "classification": "ENVIRONMENTAL_LIMITATION", "evidence_ref": "E1",
            "acceptance_impact": "AFFECTED", "explanation": "no live PubMed"}]
    rig.replace("execute", {"summary": "ok", "changed_files": [], "checks": [
        *ok_checks(), {"name": "markdown format", "status": "FAIL", "summary": "fail"}]})
    rig.script("repair", {"summary": "claims it is fine", "changed_files": [], "checks": [], "reclassifications": bad})
    state = rig.controller().run()
    it = state["iterations"][0]
    assert it["evidence_state"]["markdown format"] == "FAIL"              # nothing was waived
    assert [r["why"] for r in it["repairs"][0]["reclassifications"]["rejected"]] == \
        ["EVIDENCE_REF_REQUIRED", "ACCEPTANCE_IMPACT_NOT_NONE"]
    assert state["escalation"]["code"] == ac.E_NO_PROGRESS


# ── 8. only an exhausted ladder reaches the Human Gate ──────────────────────

def test_8_human_gate_only_after_the_whole_ladder_is_exhausted(tmp_path):
    rig = stuck(Rig(tmp_path))
    rig.script("diagnose", {"summary": "d", "diagnosis": {"root_cause": "cause one"}},
               {"summary": "d", "diagnosis": {"root_cause": "cause two"}},
               {"summary": "d", "diagnosis": {"root_cause": "cause three"}})
    state = rig.controller().run()
    rows = repair_rows(state)
    assert [(r["stage"], r["profile_id"]) for r in rows] == [
        ("CURRENT", VHIGH), ("EFFORT_UP", MAX), ("DIFFICULT_IMPLEMENTER", SONNET), ("PLANNER_DIAGNOSIS", SONNET)]
    planner_diag = [e for e in state["executions"] if e["executor"] == "diagnose"][-1]
    assert planner_diag["profile"] == OPUS                                # the planner was the last automatic analysis
    assert state["status"] == ac.AWAITING_HUMAN and state["escalation"]["code"] == ac.E_NO_PROGRESS
    assert "exhausted" in state["escalation"]["detail"]
    entries = rig.ledger()
    disp = [e for e in entries if e["entry_type"] == "DISPOSITION"]
    assert len(disp) == 3 and {d["final_disposition"] for d in disp} == {"HUMAN_GATE"}
    # the Human Gate shows what the worktree holds, and separates the last repair from the whole iteration
    hold = state["hold"]
    assert hold["candidate_fingerprint"]["changed_files"] == ["src/export/core.py"]
    assert hold["iteration_changed_files"] == ["src/export/core.py"]
    assert hold["last_repair"]["code_changed"] is False and hold["last_repair"]["stage"] == "PLANNER_DIAGNOSIS"


def test_8b_ledger_records_every_required_field(tmp_path):
    rig = stuck(Rig(tmp_path))
    rig.controller().run()
    first = next(e for e in rig.ledger() if e["entry_type"] == "ESCALATION")
    for key in ("finding_ids", "previous_model_effort", "new_model_effort", "reason", "previous_result",
                "code_state_changed", "evidence_state_changed", "final_disposition"):
        assert key in first
    assert first["previous_model_effort"]["effort"] == "xhigh" and first["new_model_effort"]["effort"] == "max"
    results = [e for e in rig.ledger() if e["entry_type"] == "RESULT" and e["escalation_id"] == first["escalation_id"]]
    assert results and results[0]["new_result"]["remaining_finding_keys"] == ["F1"]
    assert rig.journal("REPAIR_ESCALATED")


def test_8c_a_resolved_escalation_is_recorded_as_resolved(tmp_path):
    rig = Rig(tmp_path)
    rig.h.scripts["review"] = [dict(FINDING), dict(FINDING), {"verdict": "PASS"}]
    rig.script("repair", {"summary": "first try", "checks": ok_checks()},
               {"summary": "root cause was in the date window", "checks": ok_checks(),
                "diagnosis": {"root_cause": "date window"}})
    state = rig.controller().run()
    assert state["iterations"][0]["outcome"] == "PASS"
    assert [e["final_disposition"] for e in rig.ledger() if e["entry_type"] == "DISPOSITION"] == ["RESOLVED"]


# ── progress is not "files changed" ─────────────────────────────────────────

def test_progress_signal_earns_a_retry_on_the_same_stage_not_an_escalation(tmp_path):
    rig = Rig(tmp_path)
    rig.h.scripts["review"] = [dict(FINDING)]
    rig.script("repair",
               {"summary": "narrowed it down", "checks": ok_checks(), "diagnosis": {"root_cause": "cause A"}},
               {"summary": "narrowed it down further", "checks": ok_checks(), "diagnosis": {"root_cause": "cause B"}},
               {"summary": "same again", "checks": ok_checks()})
    state = rig.controller().run()
    stages = [r["stage"] for r in repair_rows(state)]
    # two attempts on CURRENT (progress earns the retry; the retry already runs at policy's "hard" profile,
    # so there is no higher effort level left and the ladder goes straight to the difficult implementer)
    assert stages[:3] == ["CURRENT", "CURRENT", "DIFFICULT_IMPLEMENTER"]


def test_repeating_the_previous_answer_is_flagged_and_gets_no_credit(tmp_path):
    rig = stuck(Rig(tmp_path))
    state = rig.controller().run()
    assert repair_rows(state)[1]["repeated_response"] is True


def test_changed_code_alone_with_the_same_finding_still_counts_as_no_progress(tmp_path):
    rig = Rig(tmp_path)
    rig.h.scripts["review"] = [dict(FINDING)]

    def churn(ctx):
        ctx["env"].diff_text += "+churn\n"
        return {"summary": f"tweak {len(ctx['env'].diff_text)}", "checks": ok_checks(), "changed_files": ["src/export/core.py"]}
    rig.script("repair", churn)
    state = rig.controller().run()
    rows = repair_rows(state)
    assert rows[0]["code_changed"] is True and rows[0]["signals"] == []      # a diff is not progress by itself
    assert rows[1]["stage"] == "EFFORT_UP"
    assert state["escalation"]["code"] == ac.E_NO_PROGRESS


def test_escalation_can_be_disabled_to_restore_the_old_single_repair_stop(tmp_path):
    rig = stuck(Rig(tmp_path, roles=roles_with(escalation={"enabled": False})))
    state = rig.controller().run()
    assert state["escalation"]["code"] == ac.E_NO_PROGRESS and rig.calls.count("repair") == 1


def test_a_completely_different_finding_after_a_repair_starts_a_fresh_ladder(tmp_path):
    rig = Rig(tmp_path)
    rig.h.scripts["review"] = [dict(FINDING), {"verdict": "REPAIR_REQUIRED", "findings": [
        {"finding_key": "OTHER", "severity": "HIGH", "summary": "new problem"}]}, {"verdict": "PASS"}]
    rig.script("repair", {"summary": "fixed F1", "checks": ok_checks()}, {"summary": "fixed OTHER", "checks": ok_checks()})
    state = rig.controller().run()
    assert [r["stage"] for r in repair_rows(state)] == ["CURRENT", "CURRENT"] and state["iterations"][0]["outcome"] == "PASS"


def test_repair_limit_still_bounds_unrelated_repair_loops(tmp_path):
    rig = Rig(tmp_path, mandate=one_item(max_repair_attempts=2))
    counter = iter(range(100))
    rig.h.scripts["review"] = [lambda ctx: {"verdict": "REPAIR_REQUIRED", "findings": [
        {"finding_key": f"K{next(counter)}", "severity": "HIGH", "summary": "a different problem every time"}]}]
    rig.script("repair", {"summary": "tried", "checks": ok_checks()})
    state = rig.controller().run()
    assert state["escalation"]["code"] == ac.E_REPAIR_LIMIT and rig.calls.count("repair") == 2


# ── compact repair packet ───────────────────────────────────────────────────

def test_later_attempts_receive_a_compact_packet_not_the_whole_context(tmp_path):
    rig = Rig(tmp_path)
    finding = {"verdict": "REPAIR_REQUIRED", "findings": [
        {"finding_key": "R8", "severity": "HIGH", "summary": "AC8 post-install checks are not demonstrated",
         "file": "src/export/core.py"}]}
    rig.h.scripts["review"] = [finding]
    rig.script("repair", {"summary": "tried", "checks": ok_checks()})
    rig.controller().run()
    first, second = rig.h.ctxs["repair"][0], rig.h.ctxs["repair"][1]
    assert first["repair_packet"] is None                       # the first attempt works from the finding itself
    packet = second["repair_packet"]
    assert packet["packet"] == "COMPACT_REPAIR_PACKET"
    assert [f["finding_key"] for f in packet["FINDINGS"]] == ["R8"]
    assert packet["PRIOR_ATTEMPTS"][0]["summary"] == "tried" and packet["PRIOR_ATTEMPTS"][0]["stage"] == "CURRENT"
    assert packet["RELEVANT_FILES"] == ["src/export/core.py"]
    handoff = aa.build_handoff("repair", {**second, "mandate": second.get("mandate", rig.h.mandate),
                                          "execution": {"run_id": "R", "iteration_id": "I", "execution_id": "E",
                                                        "descriptor_path": "d"}})
    assert "REPAIR_PACKET" in handoff and "DIFF" not in handoff and handoff["PLAN"].keys() == {"goal"}


def test_packet_picks_only_the_acceptance_criterion_a_finding_names():
    criteria = [f"AC{i}: criterion number {i}" for i in range(1, 11)]
    picked = rx.relevant_criteria([{"finding_key": f"SELF_VERIFY::{AC8}", "summary": "x"}], criteria)
    assert picked == ["AC8: criterion number 8"]
    packet = rx.build_repair_packet(
        findings=[{"finding_key": f"SELF_VERIFY::{AC8}", "severity": "HIGH", "summary": "failing"}], criteria=criteria,
        checks=[{"name": AC8, "status": "FAIL", "summary": "exit 1 " + "x" * 5000, "command": "pytest tests/post_install",
                 "exit_code": 1}],
        evidence_state={AC8: "FAIL"}, changed_files=["a.py", "b.py"],
        diff="diff --git a/a.py b/a.py\n+1\ndiff --git a/b.py b/b.py\n+2\n", prior_attempts=[])
    assert packet["FAILING_CHECKS"][0]["command"] == "pytest tests/post_install"
    assert len(packet["FAILING_CHECKS"][0]["output"]) < 1700


# ── quota routing inside the loop ───────────────────────────────────────────

def telemetry(**pools):
    return lambda: {"pools": {name: {"limits": [{"window": "5h", "certainty": "EXACT", "remaining_percent": pct}]}
                              for name, pct in pools.items()}}


def test_9_quota_shortage_routes_the_escalated_repair_to_an_equal_or_stronger_model(tmp_path):
    rig = stuck(Rig(tmp_path, quota=telemetry(**{"openai-codex": 8, "anthropic-claude": 85})))
    state = rig.controller().run()
    routed = [e for e in state["executions"] if e["executor"] == "repair"]
    # the escalated step prefers the MAX profile; its pool is low, so a class-4+ alternative serves instead
    escalated = next(e for e in routed if e["selection"]["selection_reason"].startswith("ROUTED:"))
    assert escalated["selection"]["routing"]["reason_code"] in ("QUOTA_LOW_ALTERNATIVE", "HYSTERESIS_HOLD")
    assert "QUOTA_LOW_ALTERNATIVE" in [d["payload"]["reason_code"] for d in rig.journal("ROUTING_DECISION")]
    traits = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text())["routing"]["profiles"]
    assert traits[escalated["profile"]]["pool"] == "anthropic-claude"
    assert traits[escalated["profile"]]["capability_class"] >= traits[MAX]["capability_class"]
    decision = [e for e in rig.journal("ROUTING_DECISION") if e["payload"]["executor"] == "repair"]
    assert decision and decision[-1]["payload"]["selected_quota"]["certainty"] in ("EXACT", "ESTIMATED")
    assert state["router_state"]["demoted"].get("openai-codex")


def test_9b_without_telemetry_nothing_is_rerouted(tmp_path):
    rig = stuck(Rig(tmp_path))
    state = rig.controller().run()
    assert all(not e["selection"]["selection_reason"].startswith("ROUTED:") for e in state["executions"]
               if e["executor"] == "repair" and e["selection"])
    decisions = rig.journal("ROUTING_DECISION")
    assert decisions and all(d["payload"]["selected_quota"]["certainty"] == "UNKNOWN" for d in decisions)


def antigravity_routing():
    routing = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text())["routing"]
    routing["profiles"]["ANTIGRAVITY_FREE"]["available"] = True    # as if the live integration were verified
    return routing


class Boom(aa.ExecutorFailure):
    pass


def test_10_failed_experimental_provider_does_not_stop_the_workflow(tmp_path):
    rig = Rig(tmp_path, roles=roles_with(repair_default="ANTIGRAVITY_FREE", routing=antigravity_routing()))
    rig.h.scripts["review"] = [dict(FINDING), {"verdict": "PASS"}]
    seen = []

    def repair(ctx):
        seen.append(ctx["binding"]["profile_id"])
        if ctx["binding"]["profile_id"] == "ANTIGRAVITY_FREE":
            raise ctl.ExecutorFailure("antigravity: provider process exited rc=1", dispatched=True, failure_class="TIMEOUT")
        return {"summary": "fixed by the fallback", "checks": ok_checks()}
    rig.script("repair", repair)
    state = rig.controller().run()
    assert seen[0] == "ANTIGRAVITY_FREE" and len(seen) == 2 and seen[1] != "ANTIGRAVITY_FREE"
    assert state["iterations"][0]["outcome"] == "PASS" and state["escalation"] is None
    failover = rig.journal("PROVIDER_FAILOVER")[0]["payload"]
    assert failover["failed_profile_id"] == "ANTIGRAVITY_FREE" and failover["failure_class"] == "TIMEOUT"
    assert state["router_state"]["health"]["google-antigravity"]["status"] == "TIMEOUT"
    failed = next(e for e in state["executions"] if e["profile"] == "ANTIGRAVITY_FREE")
    assert failed["selection"]["routing"]["alternatives"]                                 # the alternatives were audited


def test_failover_is_refused_when_the_failed_attempt_already_changed_the_worktree(tmp_path):
    rig = Rig(tmp_path, roles=roles_with(repair_default="ANTIGRAVITY_FREE", routing=antigravity_routing()))
    rig.h.scripts["review"] = [dict(FINDING), {"verdict": "PASS"}]

    def repair(ctx):
        ctx["env"].diff_text += "+half done\n"           # partial effects: a blind retry elsewhere would compound them
        raise ctl.ExecutorFailure("timed out", dispatched=True, failure_class="TIMEOUT")
    rig.script("repair", repair)
    state = rig.controller().run()
    assert state["escalation"]["code"] == ac.E_EXECUTOR and not rig.journal("PROVIDER_FAILOVER")


@pytest.mark.parametrize("failure", ["RATE_LIMIT", "AUTH"])
def test_hard_failures_route_around_the_provider_for_later_calls_too(tmp_path, failure):
    rig = Rig(tmp_path, roles=roles_with(repair_default="ANTIGRAVITY_FREE", routing=antigravity_routing()))
    rig.h.scripts["review"] = [dict(FINDING), dict(FINDING), {"verdict": "PASS"}]
    seen = []

    def repair(ctx):
        seen.append(ctx["binding"]["profile_id"])
        if ctx["binding"]["profile_id"] == "ANTIGRAVITY_FREE":
            raise ctl.ExecutorFailure("provider refused", dispatched=True, failure_class=failure)
        return {"summary": f"fixed {len(seen)}", "checks": ok_checks(), "diagnosis": {"root_cause": f"c{len(seen)}"}}
    rig.script("repair", repair)
    state = rig.controller().run()
    assert state["iterations"][0]["outcome"] == "PASS"
    assert seen.count("ANTIGRAVITY_FREE") == 1          # not retried while its health block is active


def test_router_disabled_keeps_the_policy_profile_and_writes_no_routing_events(tmp_path):
    routing = antigravity_routing()
    routing["policy"]["enabled"] = False
    rig = stuck(Rig(tmp_path, roles=roles_with(routing=routing), quota=telemetry(**{"openai-codex": 1})))
    state = rig.controller().run()
    assert not rig.journal("ROUTING_DECISION")
    assert all(e["profile"] in (VHIGH, MAX, SONNET, OPUS) for e in state["executions"] if e["executor"] == "repair")


def test_no_routing_block_is_backward_compatible(tmp_path):
    rig = stuck(Rig(tmp_path, roles=roles_with(drop_routing=True)))
    state = rig.controller().run()
    assert not rig.journal("ROUTING_DECISION") and state["escalation"]["code"] == ac.E_NO_PROGRESS


def test_manual_override_profile_wins_over_the_heuristics(tmp_path):
    routing = antigravity_routing()
    mandate = one_item()
    mandate["model_policy_overrides"] = {"implementation": {"profile_key": "implementer_capability_escalation",
                                                            "reason": "human asked for the stronger coder"}}
    rig = Rig(tmp_path, roles=roles_with(routing=routing), mandate=mandate, quota=telemetry(**{"anthropic-claude": 2}))
    state = rig.controller().run()
    execute = next(e for e in state["executions"] if e["executor"] == "execute")
    assert execute["profile"] == SONNET and execute["selection"]["routing"]["reason_code"] == "MANUAL_OVERRIDE"


def test_unrunnable_preferred_profile_still_fails_closed_unless_substitution_is_enabled(tmp_path):
    for substitute, expected in ((False, ac.E_ROLE_UNAVAILABLE), (True, None)):
        routing = antigravity_routing()
        routing["substitute_unavailable"] = substitute
        rig = Rig(tmp_path / str(substitute), roles=roles_with(routing=routing))
        rig.h.scripts["review"] = [dict(FINDING), {"verdict": "PASS"}]
        rig.script("repair", {"summary": "fixed", "checks": ok_checks()})
        executors = rig.h.executors()
        inner = executors["repair"]

        class Unrunnable:
            fixture_class = "SCRIPTED"

            @staticmethod
            def preflight(binding):
                return "model not available" if binding["profile_id"] == VHIGH else None

            def __call__(self, ctx, inner=inner):
                return inner(ctx)
        executors["repair"] = Unrunnable()
        c = ctl.AutonomyController.start("RUN1", rig.h.mandate, executors=executors, env=rig.env, roles=rig.h.roles,
                                         stats_root=rig.h.stats)
        state = c.run()
        if expected:
            assert state["escalation"]["code"] == expected
        else:
            assert state["escalation"] is None and state["iterations"][0]["outcome"] == "PASS"


# ── Human Gate presentation and a routing dead end ──────────────────────────

def test_human_gate_candidate_lists_changed_files_and_separates_the_last_repair(tmp_path, monkeypatch):
    import product_view
    monkeypatch.setattr(product_view.prun, "product_dir", lambda run_id: tmp_path / "product")
    rig = stuck(Rig(tmp_path))
    state = rig.controller().run()
    gate = product_view.human_gate("RUN1", state, {"workspace": {"branch": "aaw/x", "worktree": "W"}}, {})
    candidate = gate["candidate"]
    assert candidate["changed_files"] == ["src/export/core.py"]            # the whole iteration, never empty on an escalation
    assert candidate["last_repair"]["code_changed"] is False                # ... even though the last REPAIR changed nothing
    assert "eskalacji" in gate["why"] and gate["actions"]["accept"] is False


def test_no_adequate_profile_stops_before_dispatch_with_a_routing_code(tmp_path):
    rig = Rig(tmp_path, quota=telemetry(**{"openai-codex": 3, "anthropic-claude": 2, "google-antigravity": 1}))
    state = rig.controller().run()
    assert state["escalation"]["code"] == ac.E_ROUTING and state["executions"] == []
    assert "RESERVE_PROTECTED" in state["escalation"]["detail"]


def test_experimental_antigravity_is_never_picked_up_while_declared_unavailable(tmp_path):
    rig = stuck(Rig(tmp_path, quota=telemetry(**{"openai-codex": 7, "anthropic-claude": 1})))
    state = rig.controller().run()
    assert all(e["profile"] != "ANTIGRAVITY_FREE" for e in state["executions"])


def test_classified_limitations_are_listed_at_the_gate_not_hidden(tmp_path, monkeypatch):
    import product_view
    monkeypatch.setattr(product_view.prun, "product_dir", lambda run_id: tmp_path / "product")
    state = {"status": ac.AWAITING_HUMAN, "hold": {"reason": ac.HOLD_ROADMAP_EXHAUSTED, "promotable": True,
                                                    "candidate_fingerprint": {"changed_files": ["a.py"]}},
             "escalation": None, "mandate": {"roadmap_mandate": {"items": []}}, "roadmap": {},
             "iterations": [{"index": 1, "status": "ACCEPTED", "plan": {"goal": "g"}, "repairs": [],
                             "classified": [{"id": "markdown format", "classification": "PRE_EXISTING_BASELINE",
                                             "description": "3 files fail on base", "evidence_ref": "BASE#1"}]}]}
    gate = product_view.human_gate("RUN_LONG_ID_1", state, {}, {})
    assert any("markdown format" in w and "PRE_EXISTING_BASELINE" in w and "BASE#1" in w for w in gate["warnings"])
