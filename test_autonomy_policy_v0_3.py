"""Deterministic coverage for the V0.3 default policy and review boundary."""

import copy
import json
from pathlib import Path

import pytest

import autonomy_adapters as aa
import autonomy_contract as ac
import autonomy_controller as ctl
import autonomy_policy as ap
from test_autonomy import FakeEnv, Harness, mandate_fixture, ok_checks, plan

ROOT = Path(__file__).parent
IDS = {key: f"PROFILE::{key}" for key in ap.PROFILE_KEYS}


def production_roles():
    roles = ac.load_roles(ROOT / "AUTONOMY_ROLES.json", ROOT / "IMPLEMENTER_PROFILES.json")
    roles.pop("chain", None)   # these tests pin the classic per-iteration cycle; chain mode has its own tests
    return roles


def policy_harness(tmp_path, *, mandate=None, env=None):
    h = Harness(tmp_path, mandate=mandate, env=env)
    h.roles = production_roles()
    return h.defaults()


def valid_pretreatment(_ctx=None):
    return {"summary": "organized source-backed evidence", "implementation_claims": ["implemented"],
            "check_refs": ["unit"], "finding_refs": [], "changed_files": ["src/export/core.py"],
            "source_refs": ["RAW_DIFF"]}


def frozen_charter(ctx):
    mandate = ctx["mandate"]
    source = mandate["roadmap_mandate"]
    return {"mandate_hash": mandate["mandate_hash"], "objective": source["objective"],
            "roadmap_items": [{"item_id": item["item_id"], "title": item["title"],
                               "depends_on": list(item.get("depends_on", [])),
                               "human_required": item.get("human_required") is True}
                              for item in source["items"]],
            "acceptance_criteria": list(mandate["iteration_contract"]["acceptance_criteria"]),
            "boundaries": {"scope": list(mandate["iteration_contract"].get("scope", [])),
                           "constraints": list(mandate["iteration_contract"].get("constraints", [])),
                           "forbidden_changes": list(mandate["iteration_contract"].get("forbidden_changes", [])),
                           "allowed_areas": source["autonomy_bounds"].get("allowed_areas"),
                           "forbidden_areas": list(source["autonomy_bounds"].get("forbidden_areas", []))},
            "human_gate_conditions": ["ROADMAP_EXHAUSTED", "SCOPE_CHANGE", "ROLE_PROFILE_UNAVAILABLE",
                                      "PROMOTION_REQUIRES_HUMAN"], "risk_guidance": []}


def initial_plan(refs):
    def build(ctx):
        out = plan(refs)(ctx)
        out["directional_charter"] = frozen_charter(ctx)
        return out
    return build


def continuation_plan(refs, **overrides):
    def build(ctx):
        out = plan(refs, directional_charter_hash=ctx["directional_charter_hash"], **overrides)(ctx)
        return out
    return build


def prepared(h):
    h.script("prepare_packet", valid_pretreatment)
    return h


def test_01_normal_implementation_selects_luna_high():
    assert ap.select_implementation(IDS, "NORMAL")["profile_id"] == IDS["implementer_default"]


def test_02_harder_implementation_selects_luna_very_high():
    assert ap.select_implementation(IDS, "HARDER")["profile_id"] == IDS["implementer_harder"]


def test_03_significantly_difficult_implementation_selects_the_strong_implementer():
    assert ap.select_implementation(IDS, "SIGNIFICANTLY_DIFFICULT")["profile_id"] == IDS["implementer_strong"]


def test_03b_a_run_frozen_without_the_strong_slot_keeps_luna_max():
    legacy = {k: v for k, v in IDS.items() if k not in ap.OPTIONAL_PROFILE_FALLBACKS}
    assert ap.select_implementation(legacy, "SIGNIFICANTLY_DIFFICULT")["profile_id"] == IDS["implementer_hard"]
    assert ap.select_continuation_planner(legacy, {"tier": "DEFAULT"})["profile_id"] == IDS["final_review_default"]
    assert ap.validate_policy_ids(legacy)["implementer_strong"] == IDS["implementer_hard"]


def test_04_a_hard_label_or_evidence_alone_never_selects_sonnet():
    result = ap.select_implementation(IDS, "SIGNIFICANTLY_DIFFICULT", evidence=["large change"])
    assert result["profile_id"] == IDS["implementer_strong"] != IDS["implementer_capability_escalation"]
    assert result["selection_reason"] == "IMPLEMENTATION_COMPLEXITY_ESCALATION"


def test_05_sonnet_requires_a_failed_luna_max_attempt_and_capability_evidence():
    previous = {"profile_id": IDS["implementer_hard"], "outcome": "FAILED",
                "finding_code": "IMPLEMENTATION_CAPABILITY_MISMATCH", "execution_id": "EXE_1",
                "evidence_ref": "EXECUTION_RESULT:EXE_1"}
    result = ap.select_implementation(IDS, "SIGNIFICANTLY_DIFFICULT", previous_attempt=previous)
    assert result["profile_id"] == IDS["implementer_capability_escalation"]
    assert result["selection_reason"] == "LUNA_MAX_CAPABILITY_FAILURE"
    assert result["previous_attempt"] == previous and result["escalated_from"] == IDS["implementer_hard"]


def test_06_sonnet_does_not_follow_a_different_profile_failure():
    previous = {"profile_id": IDS["implementer_harder"], "outcome": "FAILED",
                "finding_code": "IMPLEMENTATION_CAPABILITY_MISMATCH", "execution_id": "EXE_1",
                "evidence_ref": "EXECUTION_RESULT:EXE_1"}
    assert ap.select_implementation(IDS, "SIGNIFICANTLY_DIFFICULT", previous_attempt=previous)["profile_id"] \
        == IDS["implementer_strong"]


def test_07_human_override_is_explicit_and_audited():
    override = {"profile_key": "implementer_capability_escalation", "reason": "human requested stronger coding"}
    result = ap.select_implementation(IDS, "NORMAL", human_override=override)
    assert result["profile_id"] == IDS["implementer_capability_escalation"]
    assert "human requested stronger coding" in result["complexity_risk_evidence"]


def test_08_pretreatment_and_primary_review_have_the_configured_default_tiers():
    roles = production_roles()
    ids = ap.profile_id_map(roles["policy_profiles"])
    assert ids["review_pretreatment"] == "GPT6_LUNA_VERY_HIGH"
    assert ids["primary_reviewer"] == "SOL_6_1_LIGHT"


def test_09_normal_repair_uses_luna_very_high():
    assert ap.select_repair(IDS, attempt=1, findings=[])["profile_id"] == IDS["repair_default"]


def test_10_harder_repair_uses_luna_max():
    finding = {"finding_key": "F1", "severity": "HIGH", "blocking": True}
    assert ap.select_repair(IDS, attempt=1, findings=[finding, {**finding, "finding_key": "F2"}])["profile_id"] \
        == IDS["repair_hard"]


def test_11_repair_after_matching_luna_max_failure_may_use_sonnet():
    previous = {"profile_id": IDS["implementer_hard"], "outcome": "FAILED", "execution_id": "EXE_1"}
    finding = {"finding_key": "F1", "severity": "HIGH", "blocking": True,
               "finding_code": "IMPLEMENTATION_CAPABILITY_MISMATCH", "evidence_ref": "EXECUTION_RESULT:EXE_1"}
    assert ap.select_repair(IDS, attempt=1, findings=[finding], previous_attempt=previous)["profile_id"] \
        == IDS["implementer_capability_escalation"]


def test_12_final_review_default_is_sol_light():
    assert ap.select_final_review(IDS, changed_files=["src/export.py"], repair_attempts=0,
                                  findings={})["profile_id"] == IDS["final_review_default"]


def test_13_architecture_review_escalates_to_sonnet_medium():
    result = ap.select_final_review(IDS, changed_files=["autonomy_controller.py"], repair_attempts=0,
                                    findings=[])
    assert result["profile_id"] == IDS["final_review_hard"]
    assert result["selection_reason"] == "FINAL_REVIEW_ARCHITECTURE_RISK"


def test_14_critical_review_escalates_to_opus_medium():
    result = ap.select_final_review(IDS, changed_files=["src/export.py"], repair_attempts=0,
                                    findings=[{"finding_key": "F1", "severity": "CRITICAL"}])
    assert result["profile_id"] == IDS["final_review_critical"]


def test_15_continuation_planner_pairs_with_the_final_review_tier():
    # DEFAULT tier: the dedicated continuation planner (writes concrete work packets), not the light final reviewer.
    for tier, key, planner in (("DEFAULT", "final_review_default", "continuation_planner"),
                               ("HARD", "final_review_hard", "final_review_hard"),
                               ("CRITICAL", "final_review_critical", "final_review_critical")):
        result = ap.select_continuation_planner(IDS, {"tier": tier, "profile_id": IDS[key],
                                                       "iteration_id": "ITER_1"})
        assert result["profile_id"] == IDS[planner]


def test_16_profile_catalog_maps_logical_tiers_without_controller_runtime_ids():
    profiles = {row["profile_id"]: row for row in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())[
        "profiles"]}
    assert (profiles["GPT6_LUNA_HIGH"]["runtime_model_id"], profiles["GPT6_LUNA_HIGH"]["effort"]) == (
        "gpt-6-luna", "high")
    assert (profiles["GPT6_LUNA_VERY_HIGH"]["runtime_model_id"], profiles["GPT6_LUNA_VERY_HIGH"]["effort"]) == (
        "gpt-6-luna", "xhigh")
    assert (profiles["GPT6_LUNA_MAX"]["runtime_model_id"], profiles["GPT6_LUNA_MAX"]["effort"]) == (
        "gpt-6-luna", "max")
    for profile_id in ("OPUS_5_5_HIGH", "OPUS_5_5_MEDIUM", "SONNET_5_5_MEDIUM"):
        assert profiles[profile_id]["availability"] == "KNOWN_BUT_UNAVAILABLE"
        assert "runtime_model_id" not in profiles[profile_id]


def test_17_initial_architect_profile_is_opus_high_once_and_human_gate_stays_last(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = prepared(policy_harness(tmp_path, mandate=m)).script("plan", initial_plan(["A"]))
    state = h.controller(with_prep=True).run()
    initial = [row for row in state["executions"] if row["role"] == "initial_planner"]
    assert len(initial) == 1 and initial[0]["profile"] == "OPUS_5_5_HIGH"
    assert state["planner_invocation_count"] == 1
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED
    assert state["hold"]["promotable"] is True


def test_18_next_iteration_uses_frozen_charter_and_does_not_repeat_initial_opus(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "core"}, {"item_id": "B", "title": "next"}]
    h = prepared(policy_harness(tmp_path, mandate=m))
    h.script("plan", initial_plan(["A"]), continuation_plan(["B"]))
    state = h.controller(with_prep=True).run()
    plan_calls = [row for row in state["executions"] if row["executor"] == "plan"]
    assert [row["role"] for row in plan_calls] == ["initial_planner", "continuation_planner"]
    assert [row["profile"] for row in plan_calls] == ["OPUS_5_5_HIGH", "SOL_6_1_MEDIUM"]
    assert len({it["lineage"]["directional_charter_hash"] for it in state["iterations"]}) == 1
    assert state["planner_invocation_count"] == 1


def test_19_planner_work_outside_the_frozen_roadmap_escalates(tmp_path):
    h = prepared(policy_harness(tmp_path))
    h.script("plan", initial_plan(["A"]), continuation_plan(["UNLISTED"]))
    state = h.controller(with_prep=True).run()
    assert state["status"] == ac.AWAITING_HUMAN
    assert state["escalation"]["code"] == ac.E_LINK
    assert len(state["iterations"]) == 1


def test_20_exact_unavailable_profile_fails_closed_without_substitution(tmp_path):
    h = policy_harness(tmp_path)
    executors = h.executors()

    class UnavailableInitial:
        def preflight(self, binding):
            assert binding["profile_id"] == "OPUS_5_5_HIGH"
            return "exact Claude 5.5 profile is unavailable"

        def __call__(self, _ctx):
            raise AssertionError("unavailable provider must not be called")

    executors["plan"] = UnavailableInitial()
    c = ctl.AutonomyController.start("RUN1", h.mandate, executors=executors, env=h.env,
                                     roles=h.roles, stats_root=h.stats)
    state = c.run()
    assert state["escalation"]["code"] == ac.E_ROLE_UNAVAILABLE and state["executions"] == []
    assert not (h.stats / "RUN1" / "EXECUTIONS").exists()


def test_21_blocking_failed_check_is_preserved_by_deterministic_packet_building():
    iteration = {"plan": {"goal": "export", "acceptance_criteria": ["export works"],
                          "decisions": [], "roadmap_refs": ["A"], "scope_justification": "A"},
                 "execution": {"summary": "implemented"}, "repairs": []}
    checks = [{"name": "unit", "status": "FAIL", "summary": "1 failed", "log_ref": "logs/unit.txt"}]
    adverse = ac.adverse_items(execution=iteration["execution"], checks=checks)
    packet = ac.build_review_packet(iteration=iteration, mandate=ac.validate_mandate(mandate_fixture()),
        diff_sha256="sha256:diff", changed_files=["src/export.py"], head="HEAD", base="BASE", worktree="WT",
        checks=checks, adverse=adverse)
    compressed = copy.deepcopy(packet)
    compressed["RISKS"]["adverse_items"] = []
    preserved = ac.enforce_adverse_preservation(compressed, adverse)
    assert any(row["status"] == "FAIL" and row["name"] == "unit" for row in preserved["RISKS"]["adverse_items"])
    assert preserved["integrity"]["reinjected"]


def test_22_blocking_finding_is_preserved_in_packet_risks():
    finding = {"finding_key": "BLOCK-1", "severity": "HIGH", "blocking": True, "summary": "must fix"}
    adverse = ac.adverse_items(execution={}, checks=[], prior_findings=[finding])
    assert any(row["source"] == "PRIOR_FINDING" and row["finding_key"] == "BLOCK-1" for row in adverse)


def test_23_warning_is_kept_with_its_raw_log_reference():
    adverse = ac.adverse_items(execution={}, checks=[{"name": "lint", "status": "PASS",
        "warnings": ["warning: deprecated"], "log_ref": "logs/lint.txt"}])
    warning = next(row for row in adverse if row["name"] == "lint")
    assert warning["warnings"] == ["warning: deprecated"] and warning["log_ref"] == "logs/lint.txt"


def test_24_primary_review_gets_a_manifest_but_no_raw_bytes_by_default(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = prepared(policy_harness(tmp_path, mandate=m)).script("plan", initial_plan(["A"]))
    c = h.controller(with_prep=True)
    c.run()
    ctx = h.ctxs["review"][0]
    handoff = aa.build_handoff("review", ctx)
    assert handoff["RAW_EVIDENCE"] == []
    assert "FROZEN_DIRECTIONAL_CHARTER_HASH" in handoff
    assert any(row["source_ref"] == "RAW_DIFF" for row in handoff["RAW_EVIDENCE_MANIFEST"])
    assert "diff" not in ctx["raw"] and Path(ctx["raw"]["diff_path"]).is_file()


def test_25_pretreatment_cannot_return_an_authoritative_verdict():
    assert not ctl.AutonomyController._valid_pretreatment({**valid_pretreatment(), "verdict": "PASS"})
    assert not ctl.AutonomyController._valid_pretreatment({**valid_pretreatment(), "metadata": {"decision": "PASS"}})


def test_26_ambiguous_review_can_request_one_targeted_raw_source(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = prepared(policy_harness(tmp_path, mandate=m)).script("plan", initial_plan(["A"]))
    h.scripts["review"] = [{"verdict": "ESCALATE", "summary": "diff context needed", "findings": [],
                             "raw_evidence_requests": [{"source_ref": "RAW_DIFF", "reason": "changed lines are ambiguous"}]},
                            {"verdict": "PASS", "summary": "verified raw diff", "findings": []}]
    state = h.controller(with_prep=True).run()
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"]
    events = ctl.AutonomyJournal(h.stats / "RUN1" / "AUTONOMY" / "autonomy_events.jsonl", "RUN1").read()
    assert [row["event_type"] for row in events].count("RAW_EVIDENCE_REQUESTED") == 1
    provided = next(row for row in events if row["event_type"] == "RAW_EVIDENCE_PROVIDED")
    assert provided["payload"]["source_ref"] == "RAW_DIFF"
    retry_context = h.ctxs["review"][1]
    source_path = Path(next(row["path"] for row in retry_context["raw"]["manifest"]
                            if row["source_ref"] == "RAW_DIFF"))
    assert retry_context["raw_evidence_results"][0]["content"] == source_path.read_bytes().decode("utf-8")


def test_27_clear_review_does_not_request_or_receive_raw_evidence(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = prepared(policy_harness(tmp_path, mandate=m)).script("plan", initial_plan(["A"]))
    state = h.controller(with_prep=True).run()
    ctx = h.ctxs["review"][0]
    assert "raw_evidence_results" not in ctx
    assert not any(row["event_type"] == "RAW_EVIDENCE_REQUESTED" for row in
                   ctl.AutonomyJournal(h.stats / "RUN1" / "AUTONOMY" / "autonomy_events.jsonl", "RUN1").read())
    assert state["status"] == ac.AWAITING_HUMAN


def test_28_frozen_architect_risk_guidance_sets_policy_floors(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "architecture"}]
    h = prepared(policy_harness(tmp_path, mandate=m))

    def guided_plan(ctx):
        out = initial_plan(["A"])(ctx)
        out["directional_charter"]["risk_guidance"] = [{"item_id": "A", "implementation_floor": "HARDER",
            "final_review_floor": "HARD", "reason": "cross-module dependency risk"}]
        return out

    h.script("plan", guided_plan)
    state = h.controller(with_prep=True).run()
    execute = next(row for row in state["executions"] if row["executor"] == "execute")
    final = next(row for row in state["executions"] if row["executor"] == "final_review")
    assert execute["profile"] == "GPT6_LUNA_VERY_HIGH"
    assert final["profile"] == "SONNET_5_5_MEDIUM"
    assert final["selection"]["selection_reason"] == "DIRECTIONAL_CHARTER_RISK_FLOOR"


def test_29_unresolved_primary_review_uncertainty_escalates_final_review_tier():
    result = ap.select_final_review(IDS, changed_files=["src/export.py"], repair_attempts=0,
                                    findings=[], uncertainty=True)
    assert result["profile_id"] == IDS["final_review_hard"]
    normalized = ac.normalize_review({"verdict": "PASS", "summary": "passes with residual doubt", "findings": [],
                                      "uncertainties": ["cross-module race remains unclear"]})
    assert normalized["uncertainties"] == ["cross-module race remains unclear"]


def test_30_codex_output_schemas_require_every_declared_property_recursively():
    def inspect(node, path="$", failures=None):
        failures = failures if failures is not None else []
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                missing = set(properties) - set(node.get("required", []))
                if missing:
                    failures.append((path, sorted(missing)))
                for key, child in properties.items():
                    inspect(child, f"{path}.{key}", failures)
            if isinstance(node.get("items"), dict):
                inspect(node["items"], f"{path}[]", failures)
        return failures

    assert not {name: inspect(schema) for name, schema in aa.OUTPUT_SCHEMAS.items()
                if inspect(schema)}, "every Codex strict-schema property, including nullable keys, is required"
    review = aa.OUTPUT_SCHEMAS["review"]
    assert "raw_evidence_requests" in review["required"]
    assert "uncertainties" in review["required"]
    assert "finding_code" in review["properties"]["findings"]["items"]["required"]


def test_31_role_prompts_leave_git_boundary_checks_to_the_controller():
    assert "Do not run Git commands" in aa.ROLE_INSTRUCTIONS["execute"]
    assert "controller verifies Git boundaries" in aa.ROLE_INSTRUCTIONS["self_verify"]
    assert "Do not run Git commands" in aa.ROLE_INSTRUCTIONS["review"]


def test_32_diff_digests_are_consistent_and_labelled_for_the_reviewer(tmp_path):
    import hashlib
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = prepared(policy_harness(tmp_path, mandate=m)).script("plan", initial_plan(["A"]))
    h.controller(with_prep=True).run()
    ctx = h.ctxs["review"][0]
    raw = ctx["raw"]
    file_bytes = Path(raw["diff_path"]).read_bytes()
    assert file_bytes == h.env.diff_text.encode("utf-8")
    assert ctl.diff_digest(file_bytes.decode("utf-8")) == raw["diff_sha256"]
    manifest_sha = next(r["sha256"] for r in raw["manifest"] if r["source_ref"] == "RAW_DIFF")
    assert manifest_sha == raw["diff_file_sha256"] == hashlib.sha256(file_bytes).hexdigest()
    metadata = aa.build_handoff("review", ctx)["RAW_METADATA"]
    assert metadata["diff_file_sha256"] == manifest_sha
    assert "different algorithms" in metadata["diff_digest_note"].lower()


def test_34_implementer_is_told_to_name_checks_after_required_evidence_and_matching_stays_strict():
    assert "exact wording of the REQUIRED_EVIDENCE item" in aa.ROLE_INSTRUCTIONS["execute"]
    assert "exact command, its exit code" in aa.ROLE_INSTRUCTIONS["execute"]


def test_33_genuinely_ambiguous_review_still_escalates_fail_closed(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "only"}]
    h = prepared(policy_harness(tmp_path, mandate=m)).script("plan", initial_plan(["A"]))
    ask = {"verdict": "ESCALATE", "summary": "still unclear", "findings": [],
           "raw_evidence_requests": [{"source_ref": "RAW_DIFF", "reason": "unclear"}]}
    h.scripts["review"] = [ask, ask]
    state = h.controller(with_prep=True).run()
    assert state["status"] == ac.AWAITING_HUMAN
    assert state["escalation"]["code"] == ac.E_REVIEW and not state["hold"]["promotable"]
