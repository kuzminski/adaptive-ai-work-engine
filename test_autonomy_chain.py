"""Chain mode (autonomy_chain + controller): long chains, one serious review, deferred minor findings, polish.

Driven through the real executor boundary with scripted executors, like test_autonomy.py.
"""

import json

import pytest

import aaw_telemetry as tel
import autonomy_chain as chain
import autonomy_contract as ac
import autonomy_controller as ctl
from test_autonomy import Harness, end_plan, events, mandate_fixture, ok_checks, plan


# ── helpers ──────────────────────────────────────────────────────────────────

def chain_roles(h, **cfg):
    h.roles["chain"] = chain.normalize_config({"enabled": True, "length": 3, **cfg})
    return h


def items(*ids):
    m = mandate_fixture(max_iterations=20)
    m["roadmap_mandate"]["items"] = [{"item_id": i, "title": f"item {i}"} for i in ids]
    return m


def stub(ref, **over):
    base = {"goal": f"do {ref}", "roadmap_refs": [ref], "scope_justification": f"advances {ref}",
            "acceptance_criteria": ["it works"], "touched_areas": ["src/export"],
            "implementation_complexity": "NORMAL", "complexity_evidence": []}
    base.update(over)
    return base


def finding(key, severity, summary="x", file="src/export/core.py", **over):
    return {"finding_key": key, "severity": severity, "summary": summary, "file": file, **over}


def harness(tmp_path, *ids, **cfg):
    return chain_roles(Harness(tmp_path, mandate=items(*ids)).defaults(), **cfg)


# ── config ───────────────────────────────────────────────────────────────────

def test_config_defaults_are_disabled_and_validated():
    assert chain.normalize_config(None)["enabled"] is False
    cfg = chain.normalize_config({"enabled": True})
    assert cfg["length"] == 8 and cfg["mid_repair_min_severity"] == "CRITICAL" and cfg["close_final_floor"] == "HARD"
    assert chain.normalize_config(True)["enabled"] is True
    for bad in ({"length": 1}, {"length": "8"}, {"typo_key": 1}, {"mid_chain_review": "FULL"},
                {"mid_repair_min_severity": "LOW"}, {"close_final_floor": "SOFT"}, {"polish": {"max_findings": 0}}):
        with pytest.raises(chain.ChainConfigError):
            chain.normalize_config({"enabled": True, **bad})


def test_production_roles_file_carries_a_valid_enabled_chain_block():
    from pathlib import Path
    roles = ac.load_roles(Path(ac.__file__).parent / "AUTONOMY_ROLES.json", Path(ac.__file__).parent / "IMPLEMENTER_PROFILES.json")
    assert roles["chain"]["enabled"] is True and roles["chain"]["length"] == 8
    assert "chain" in ac.NON_ROLE_KEYS


def test_invalid_chain_config_is_rejected_by_role_validation():
    from pathlib import Path
    import json as _json
    root = Path(ac.__file__).parent
    cfg = _json.loads((root / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    cfg["chain"]["length"] = 0
    catalog = {p["profile_id"]: p for p in _json.loads((root / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
    with pytest.raises(ac.AutonomyError, match="chain config invalid"):
        ac.validate_roles(cfg, catalog)


# ── severity policy (pure) ───────────────────────────────────────────────────

def test_mid_chain_only_critical_stops_work_and_the_rest_is_deferred():
    raw = {"verdict": "REPAIR_REQUIRED", "summary": "s", "findings": [
        finding("H", "HIGH", blocking=True), finding("M", "MEDIUM"), finding("C", "CRITICAL")]}
    out, deferred, note = chain.relax_review(raw, min_repair="CRITICAL", honor_blocking_flag=False)
    assert [f["finding_key"] for f in out["findings"]] == ["C"] and out["verdict"] == "REPAIR_REQUIRED"
    assert {f["finding_key"] for f in deferred} == {"H", "M"} and all(f["blocking"] is False for f in deferred)
    assert note["deferred"] == 2


def test_repair_verdict_with_nothing_above_the_threshold_becomes_pass():
    raw = {"verdict": "REPAIR_REQUIRED", "summary": "meh", "findings": [finding("H", "HIGH", blocking=True)]}
    out, deferred, note = chain.relax_review(raw, min_repair="CRITICAL", honor_blocking_flag=False)
    assert out["verdict"] == "PASS" and out["findings"] == [] and len(deferred) == 1
    assert note["verdict_relaxed_from"] == "REPAIR_REQUIRED"
    # ... and normalize_review cannot resurrect it: the deferred HIGH is no longer in the result
    assert ac.normalize_review(out)["verdict"] == ac.V_PASS


def test_serious_review_repairs_high_and_honours_explicit_blocking_on_medium():
    raw = {"verdict": "REPAIR_REQUIRED", "summary": "s", "findings": [
        finding("H", "HIGH"), finding("MB", "MEDIUM", blocking=True), finding("ML", "MEDIUM"), finding("L", "LOW", blocking=True)]}
    out, deferred, _ = chain.relax_review(raw, min_repair="HIGH", honor_blocking_flag=True)
    assert sorted(f["finding_key"] for f in out["findings"]) == ["H", "MB"]
    assert sorted(f["finding_key"] for f in deferred) == ["L", "ML"]      # LOW is never blocking, whatever the flag says


def test_relax_review_leaves_malformed_results_to_normalize_review():
    for bad in (None, "x", {"verdict": "PASS", "findings": "nope"}, {"verdict": "PASS", "findings": ["a"]}):
        out, deferred, note = chain.relax_review(bad, min_repair="HIGH", honor_blocking_flag=True)
        assert out == bad and deferred == [] and note["relaxed"] is False


def test_backlog_deduplicates_by_key_and_file():
    backlog: list = []
    chain.defer(backlog, [finding("K", "MEDIUM")], iteration_id="I1", chain_id=1, origin="REVIEW", at="t")
    again = chain.defer(backlog, [finding("K", "MEDIUM"), finding("K", "MEDIUM", file="other.py")],
                        iteration_id="I2", chain_id=1, origin="REVIEW", at="t")
    assert len(backlog) == 2 and len(again) == 1


def test_polish_plan_respects_the_mandate_areas_and_orders_by_severity():
    mandate = ac.validate_mandate(items("A"))
    backlog = [{"finding_key": "low", "severity": "LOW", "summary": "n", "file": "src/a.py", "status": "OPEN", "at": "1"},
               {"finding_key": "med", "severity": "MEDIUM", "summary": "m", "file": "src/b.py", "status": "OPEN", "at": "2"},
               {"finding_key": "out", "severity": "MEDIUM", "summary": "o", "file": "secrets/key.py", "status": "OPEN", "at": "3"},
               {"finding_key": "done", "severity": "HIGH", "summary": "d", "file": "src/c.py", "status": "RESOLVED", "at": "4"}]
    plan_, selected, out_of_scope = chain.build_polish_plan(mandate, backlog, charter_hash=None, max_findings=2)
    assert [r["finding_key"] for r in selected] == ["med", "low"] and [r["finding_key"] for r in out_of_scope] == ["out"]
    assert plan_["roadmap_refs"] == [] and plan_["touched_areas"] == ["src/a.py", "src/b.py"]
    assert chain.build_polish_plan(mandate, backlog[2:3], charter_hash=None, max_findings=2)[0] is None


def test_clean_stubs_keeps_the_valid_prefix_and_respects_the_limit():
    good = [stub("A"), stub("B"), stub("C")]
    assert len(chain.clean_stubs(good, 2)) == 2
    assert chain.clean_stubs([stub("A"), {"goal": "broken"}, stub("C")], 5) == [chain.clean_stubs([stub("A")], 1)[0]]
    assert chain.clean_stubs("nope", 3) == [] and chain.clean_stubs(None, 3) == []


# ── controller flow ──────────────────────────────────────────────────────────

def test_chain_runs_light_reviews_inside_and_one_serious_review_at_the_end(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    c = h.controller()
    state = c.run()
    assert h.calls == ["plan", "execute", "self_verify", "review", "plan", "execute", "self_verify", "review",
                       "plan", "execute", "self_verify", "review", "final_review"]
    assert [i["status"] for i in state["iterations"]] == ["ACCEPTED"] * 3
    assert [i["chain"]["review_mode"] for i in state["iterations"]] == ["LIGHT", "LIGHT", "CHAIN_CLOSE"]
    assert state["iterations"][0]["accepted_via"]["closing_iteration_id"] == state["iterations"][2]["iteration_id"]
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["roadmap_exhausted"] and state["hold"]["promotable"]
    assert len(events(c, "ITERATION_PROVISIONALLY_ACCEPTED")) == 2 and len(events(c, "CHAIN_ACCEPTED")) == 1
    assert state["chain_state"] == {"chain_id": 2, "closed_chains": 1}


def test_classic_mode_is_unchanged_when_chain_is_disabled(tmp_path):
    h = Harness(tmp_path, mandate=items("A", "B")).defaults().script("plan", plan(["A"]), plan(["B"]))
    state = h.controller().run()
    assert h.calls.count("final_review") == 2 and all(i["status"] == "ACCEPTED" for i in state["iterations"])
    assert all(i.get("chain") is None for i in state["iterations"])


def test_mid_chain_review_none_skips_model_review_inside_the_chain(tmp_path):
    h = harness(tmp_path, "A", "B", "C", mid_chain_review="NONE").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.controller().run()
    assert h.calls.count("review") == 1 and h.calls.count("final_review") == 1     # only the closing iteration is reviewed
    assert h.calls[:4] == ["plan", "execute", "self_verify", "plan"]


def test_mid_chain_high_finding_is_deferred_and_does_not_stop_the_chain(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["review"] = [
        {"verdict": "REPAIR_REQUIRED", "summary": "s", "findings": [finding("HI", "HIGH", "bad edge case", blocking=True),
                                                                    finding("ME", "MEDIUM", "naming")]},
        {"verdict": "PASS"}, {"verdict": "PASS"}]
    c = h.controller()
    state = c.run()
    assert "repair" not in h.calls and [i["repair_attempts"] for i in state["iterations"]] == [0, 0, 0, 0]
    assert state["iterations"][3]["lineage"]["source"] == "POLISH_BACKLOG"        # the deferred items are polished last
    assert {r["finding_key"] for r in state["deferred_findings"]} >= {"HI", "ME"}
    assert events(c, "FINDINGS_DEFERRED")[0]["payload"]["verdict_relaxed_from"] == "REPAIR_REQUIRED"
    # the chain-close reviewer is shown the backlog and the whole chain
    view = h.ctxs["final_review"][0]["chain"]
    assert view["closes_chain"] and len(view["iterations"]) == 3
    assert {r["finding_key"] for r in view["deferred_findings"]} >= {"HI", "ME"}
    assert view["repair_threshold"] == "HIGH"


def test_mid_chain_critical_finding_is_repaired_immediately(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["review"] = [{"verdict": "REPAIR_REQUIRED", "summary": "s", "findings": [finding("CR", "CRITICAL", "build broken")]},
                           {"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}]
    h.script("repair", {"summary": "fixed", "checks": ok_checks()})
    state = h.controller().run()
    assert h.calls[:8] == ["plan", "execute", "self_verify", "review", "repair", "self_verify", "review", "plan"]
    assert state["iterations"][0]["repair_attempts"] == 1 and state["iterations"][0]["status"] == "ACCEPTED"


def test_chain_close_repairs_high_findings_then_passes(tmp_path):
    h = harness(tmp_path, "A", "B").script("plan", plan(["A"]), plan(["B"]))
    h.roles["chain"] = chain.normalize_config({"enabled": True, "length": 2})
    h.scripts["final_review"] = [
        {"verdict": "REPAIR_REQUIRED", "summary": "s", "findings": [finding("HI", "HIGH", "wrong total")]},
        {"verdict": "PASS"}]
    h.script("repair", {"summary": "fixed the total", "checks": ok_checks()})
    state = h.controller().run()
    assert h.calls[-5:] == ["final_review", "repair", "self_verify", "review", "final_review"]
    assert state["iterations"][1]["repairs"][0]["addresses"] == ["HI"]
    assert [i["status"] for i in state["iterations"]] == ["ACCEPTED", "ACCEPTED"] and state["escalation"] is None


def test_roadmap_ending_inside_a_chain_closes_it_with_the_serious_review(tmp_path):
    h = harness(tmp_path, "A", "B", length=8).script("plan", plan(["A"]), plan(["B"]))
    c = h.controller()
    state = c.run()
    assert h.calls[-3:] == ["review", "review", "final_review"]
    assert h.calls.count("review") == 3 and h.calls.count("final_review") == 1   # light, light, then the closing review pair
    assert [i["status"] for i in state["iterations"]] == ["ACCEPTED", "ACCEPTED"]
    started = events(c, "CHAIN_CLOSE_STARTED")
    assert len(started) == 1 and started[0]["payload"]["reason"] == "ROADMAP_EXHAUSTED"
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"]


def test_iteration_cap_also_closes_the_open_chain_before_the_human_gate(tmp_path):
    m = items("A", "B", "C", "D")
    m["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] = 2
    h = chain_roles(Harness(tmp_path, mandate=m).defaults(), length=8).script("plan", plan(["A"]), plan(["B"]))
    state = h.controller().run()
    assert state["hold"]["reason"] == ac.HOLD_ITERATION_CAP and h.calls.count("final_review") == 1
    assert [i["status"] for i in state["iterations"]] == ["ACCEPTED", "ACCEPTED"]


def test_final_review_escalation_at_chain_close_stops_for_a_human_with_the_backlog(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["review"] = [{"verdict": "PASS", "summary": "ok", "findings": [finding("ME", "MEDIUM", "naming")]}]
    h.scripts["final_review"] = [{"verdict": "ESCALATE", "summary": "needs a product decision"}]
    state = h.controller().run()
    assert state["status"] == ac.AWAITING_HUMAN and state["escalation"]["code"] == ac.E_REVIEW
    assert not state["hold"]["promotable"]
    # the work the serious review did not clear is never silently accepted
    assert [i["status"] for i in state["iterations"]] == ["PROVISIONAL", "PROVISIONAL", "ESCALATED"]


def test_a_failing_check_still_triggers_repair_inside_a_chain(tmp_path):
    h = harness(tmp_path, "A", "B", "C", mid_chain_review="NONE").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["execute"] = [{"summary": "impl", "checks": [{"name": "unit", "status": "FAIL", "summary": "red"}]},
                            {"summary": "impl", "checks": ok_checks()}]
    h.scripts["self_verify"] = [{"checks": []}]
    h.script("repair", {"summary": "fixed", "checks": ok_checks()})
    state = h.controller().run()
    assert state["iterations"][0]["repair_attempts"] == 1 and h.calls[:5] == ["plan", "execute", "self_verify", "repair", "self_verify"]


# ── polish ───────────────────────────────────────────────────────────────────

def test_polish_pass_runs_once_after_the_roadmap_without_a_planner_call(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["review"] = [{"verdict": "PASS", "summary": "ok", "findings": [finding("ME", "MEDIUM", "rename helper")]}]
    c = h.controller()
    state = c.run()
    assert h.calls.count("plan") == 3                                   # polish is controller-authored
    assert len(state["iterations"]) == 4 and state["iterations"][3]["lineage"]["source"] == "POLISH_BACKLOG"
    polish = state["iterations"][3]
    assert polish["chain"]["review_mode"] == "POLISH" and polish["status"] == "ACCEPTED"
    assert any("ME" in criterion for criterion in polish["plan"]["acceptance_criteria"])
    assert polish["plan"]["roadmap_refs"] == [] and state["polish_done"] is True
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["roadmap_exhausted"]
    # a second exhaustion does not polish again
    assert len(events(c, "POLISH_PLANNED")) == 1


def test_no_polish_when_nothing_was_deferred_or_polish_is_disabled(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    assert len(h.controller().run()["iterations"]) == 3
    h2 = chain_roles(Harness(tmp_path / "x", mandate=items("A", "B", "C")).defaults(), polish={"enabled": False, "max_findings": 5})
    h2.script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h2.scripts["review"] = [{"verdict": "PASS", "summary": "ok", "findings": [finding("ME", "MEDIUM", "x")]}]
    state = h2.controller().run()
    assert len(state["iterations"]) == 3 and state["hold"]["deferred_findings"][0]["finding_key"] == "ME"


def test_deferred_findings_reach_the_human_gate(tmp_path):
    m = items("A", "B", "C", "D")
    m["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] = 3
    h = chain_roles(Harness(tmp_path, mandate=m).defaults(), polish={"enabled": True, "max_findings": 3})
    h.script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["review"] = [{"verdict": "PASS", "summary": "ok", "findings": [finding("ME", "MEDIUM", "x")]}]
    state = h.controller().run()
    assert state["hold"]["reason"] == ac.HOLD_ITERATION_CAP         # polish only runs when the roadmap is done
    assert [r["finding_key"] for r in state["hold"]["deferred_findings"]] == ["ME"]


# ── chain plan (one strong plan, many iterations) ────────────────────────────

def test_chain_plan_lets_one_planner_call_drive_the_whole_chain(tmp_path):
    h = harness(tmp_path, "A", "B", "C")
    h.script("plan", plan(["A"], chain_plan=[stub("B"), stub("C")]))
    c = h.controller()
    state = c.run()
    assert h.calls.count("plan") == 1 and len(state["iterations"]) == 3
    assert [i["lineage"]["source"] for i in state["iterations"]] == ["ITERATION_CONTRACT", "CHAIN_PLAN", "CHAIN_PLAN"]
    assert all("chain_plan" not in i["plan"] for i in state["iterations"])        # never leaked to the implementer
    assert h.ctxs["plan"][0]["chain"]["slots_after_this"] == 2
    assert events(c, "CHAIN_PLAN_RECORDED")[0]["payload"]["kept"] == 2


def test_an_invalid_stub_falls_back_to_the_planner_without_escalating(tmp_path):
    h = harness(tmp_path, "A", "B", "C")
    h.script("plan", plan(["A"], chain_plan=[stub("B", touched_areas=["secrets/x"])]), plan(["B"]), plan(["C"]))
    c = h.controller()
    state = c.run()
    assert state["escalation"] is None and h.calls.count("plan") == 3
    assert len(events(c, "CHAIN_PLAN_STUB_REJECTED")) == 1
    assert [i["lineage"]["source"] for i in state["iterations"]][1] != "CHAIN_PLAN"


def test_stubs_that_name_an_already_finished_item_are_dropped(tmp_path):
    h = harness(tmp_path, "A", "B")
    h.script("plan", plan(["A"], chain_plan=[stub("A")]), plan(["B"]))
    state = h.controller().run()
    assert state["escalation"] is None and h.calls.count("plan") == 2


# ── telemetry ────────────────────────────────────────────────────────────────

def test_every_executor_call_leaves_one_telemetry_record_with_chain_context(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    c = h.controller()
    state = c.run()
    records = tel.read_records(c.dir / "telemetry.jsonl")
    assert len(records) == len(h.calls) == len(state["executions"])
    assert [r["executor"] for r in records] == h.calls
    assert {r["chain_id"] for r in records} == {1}
    assert {r["review_mode"] for r in records if r["executor"] == "execute"} == {"LIGHT", "CHAIN_CLOSE"}
    assert all(r["wall_s"] is not None and r["schema"] == tel.SCHEMA for r in records)
    summary = tel.summarize(records, state)
    assert summary["by_category"]["IMPLEMENTATION"]["calls"] == 3 and summary["by_category"]["REVIEW"]["calls"] == 4
    assert summary["outcome"]["accepted_or_provisional"] == 3


def test_a_broken_telemetry_sink_never_stops_a_run(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    c = h.controller()

    class Broken:
        errors = 0

        def append(self, record):
            raise OSError("disk full")

    c.telemetry = Broken()
    assert c.run()["status"] == ac.AWAITING_HUMAN


def test_telemetry_can_be_rebuilt_from_state_and_results_for_old_runs(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    c = h.controller()
    state = c.run()
    rebuilt = tel.reconstruct_from_state(state, c.results_root)
    assert [r["execution_id"] for r in rebuilt] == [e["execution_id"] for e in state["executions"]]
    assert tel.summarize(rebuilt, state)["records"] == len(state["executions"])


def test_resume_after_a_provisional_iteration_keeps_chain_state(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    c = h.controller()
    c.run()
    again = h.controller("RUN1", resume=True)
    assert again.state["chain_state"]["closed_chains"] == 1 and again.chain_cfg["enabled"] and again.chain_cfg["length"] == 3
    again.release()


# ── with the production model policy ─────────────────────────────────────────

def policy_chain(tmp_path, *ids, **cfg):
    from test_autonomy_policy_v0_3 import production_roles
    h = Harness(tmp_path, mandate=items(*ids))
    h.roles = production_roles()
    h.roles["chain"] = chain.normalize_config({"enabled": True, "length": 3, **cfg})
    return h.defaults()


def test_chain_close_review_runs_on_the_hard_tier_and_light_reviews_on_the_primary_reviewer(tmp_path):
    from test_autonomy_policy_v0_3 import initial_plan
    h = policy_chain(tmp_path, "A", "B", "C")

    def first(ctx):
        out = initial_plan(["A"])(ctx)
        out["chain_plan"] = [stub("B"), stub("C")]
        return out

    h.script("plan", first)
    c = h.controller()
    state = c.run()
    assert h.calls.count("plan") == 1 and state["status"] == ac.AWAITING_HUMAN and state["escalation"] is None
    selections = {e["payload"]["execution_id"]: e["payload"] for e in events(c, "MODEL_POLICY_SELECTED")}
    reviews = [selections[e["execution_id"]] for e in state["executions"] if e["executor"] == "review"]
    finals = [selections[e["execution_id"]] for e in state["executions"] if e["executor"] == "final_review"]
    assert {r["profile_key"] for r in reviews} == {"primary_reviewer"} and len(reviews) == 3
    assert len(finals) == 1 and finals[0]["profile_key"] == "final_review_hard"
    assert finals[0]["selection_reason"] == "CHAIN_CLOSE_FLOOR" and finals[0]["escalated_from"].startswith("SOL_5_6")


def test_an_unrunnable_floor_profile_keeps_the_default_tier_instead_of_stopping_the_run(tmp_path):
    from test_autonomy_policy_v0_3 import continuation_plan, initial_plan
    h = policy_chain(tmp_path, "A", "B", "C")
    h.script("plan", initial_plan(["A"]), continuation_plan(["B"]), continuation_plan(["C"]))
    real = h.executors

    def with_preflight(**kwargs):
        executors = real(**kwargs)

        class Gate:
            def __init__(self, fn): self.fn = fn
            def __call__(self, ctx): return self.fn(ctx)
            @staticmethod
            def preflight(binding):
                return "not installed here" if binding.get("profile_id") == "SONNET_5_5_MEDIUM" else None
        executors["final_review"] = Gate(executors["final_review"])
        return executors

    h.executors = with_preflight
    c = h.controller()
    state = c.run()
    assert state["escalation"] is None and state["status"] == ac.AWAITING_HUMAN
    assert events(c, "CHAIN_FLOOR_UNAVAILABLE")[0]["payload"]["profile_id"] == "SONNET_5_5_MEDIUM"
    final = next(e for e in state["executions"] if e["executor"] == "final_review")
    assert final["profile"] == "SOL_5_6_LIGHT"


# ── polish bookkeeping and the human-facing view ─────────────────────────────

def test_polished_findings_leave_the_open_backlog_and_the_gate_lists_only_what_remains(tmp_path):
    h = harness(tmp_path, "A", "B", "C").script("plan", plan(["A"]), plan(["B"]), plan(["C"]))
    h.scripts["review"] = [{"verdict": "PASS", "summary": "ok", "findings": [finding("ME", "MEDIUM", "rename helper")]}]
    state = h.controller().run()
    assert [r["status"] for r in state["deferred_findings"]] == ["POLISH_ATTEMPTED"]
    assert state["hold"]["deferred_findings"] == []


def test_product_view_labels_a_provisional_iteration_and_lists_deferred_findings():
    import product_view as pv
    state = {"status": ac.RUNNING, "escalation": None, "planning": None, "roadmap": {},
             "deferred_findings": [{"status": "OPEN", "severity": "MEDIUM", "summary": "rename helper", "file": "a.py"},
                                   {"status": "POLISH_ATTEMPTED", "severity": "LOW", "summary": "gone", "file": None}],
             "iterations": [{"iteration_id": "I1", "index": 1, "status": "PROVISIONAL", "repairs": [],
                             "plan": {"goal": "g"}, "lineage": {"roadmap_refs": ["A"]}}]}
    rows = pv.timeline(state, {}, {"status": pv.S_RUNNING})
    assert rows[0]["label"] == "PASS (wstępnie)"
    state["status"] = ac.AWAITING_HUMAN
    assert "bez review serii" in pv.timeline(state, {}, {"status": pv.S_GATE})[0]["label"]
