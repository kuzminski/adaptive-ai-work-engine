"""AAW implementer effectiveness V0.1: work packets, difficulty routing, final self-audit,
deterministic simple-error checks, and the Gemini CLI provider path."""

import json
import os
import sys
from pathlib import Path

import pytest

import autonomy_adapters as aa
import autonomy_contract as ac
import autonomy_controller as ctl
import product_providers as pp
import product_recommendations as pr
import work_packet as wp
from test_autonomy import FakeEnv, Harness, mandate_fixture, ok_checks
from test_autonomy_policy_v0_3 import initial_plan, production_roles

ROOT = Path(__file__).parent

PACKET = {
    "files_to_read": ["src/export/core.py"], "files_to_change": ["src/export/core.py", "tests/test_export.py"],
    "steps": [{"step_id": "S1", "action": "add the export function to core", "files": ["src/export/core.py"],
               "details": "export() returns rows as CSV lines", "verify": "python -m pytest -q tests/test_export.py"},
              {"step_id": "S2", "action": "add unit tests for export", "files": ["tests/test_export.py"],
               "details": "cover the empty input", "verify": "python -m pytest -q tests/test_export.py"}],
    "verification_commands": [{"evidence_name": "unit tests", "command": "python -m pytest -q", "expect": "exit 0"}],
    "definition_of_done": ["export() exists", "tests pass"], "pitfalls": ["Windows line endings"],
    "out_of_scope": ["docs"], "needs_strong_implementer": False, "strong_implementer_reason": None}

ALL_PASS_AUDIT = {"items": [{"id": i, "status": "PASS", "evidence": "checked"} for i in wp.AUDIT_IDS],
                  "fixed_during_audit": []}


def audit_with(**statuses):
    return {"items": [{"id": i, "status": statuses.get(i, "PASS"), "evidence": f"{i} evidence"}
                      for i in wp.AUDIT_IDS], "fixed_during_audit": []}


# ── work packet: lint and difficulty ───────────────────────────────────────

def test_complete_packet_lints_clean_and_routes_to_the_default_implementer():
    lint = wp.lint_work_packet({"work_packet": PACKET}, ["unit tests"])
    assert lint["present"] and lint["issues"] == [] and lint["uncovered_evidence"] == []
    assert lint["metrics"]["steps"] == 2 and lint["metrics"]["files"] == 2
    assert wp.assess_difficulty({"work_packet": PACKET}, lint)["route"] == wp.ROUTE_DEFAULT


def test_vague_packet_reports_every_gap():
    packet = dict(PACKET, steps=[{"step_id": "S1", "action": "fix", "files": [], "details": "", "verify": ""}],
                  files_to_change=[], verification_commands=[], definition_of_done=[])
    issues = wp.lint_work_packet({"work_packet": packet}, ["unit tests"])["issues"]
    for expected in ("STEP_WITHOUT_FILES:S1", "STEP_WITHOUT_VERIFY:S1", "STEP_ACTION_VAGUE:S1", "NO_FILES_TO_CHANGE",
                     "NO_VERIFICATION_COMMANDS", "NO_DEFINITION_OF_DONE", "EVIDENCE_WITHOUT_COMMAND:unit tests"):
        assert expected in issues


@pytest.mark.parametrize("plan, reason", [
    ({"work_packet": dict(PACKET, needs_strong_implementer=True, strong_implementer_reason="cross-module state")},
     "PLANNER_REQUESTED_STRONG"),
    ({"work_packet": dict(PACKET, files_to_change=[f"m{i}/f.py" for i in range(8)])}, "LARGE_ITERATION"),
    ({"work_packet": None}, "UNDER_SPECIFIED_PLAN"),
    ({"work_packet": PACKET, "implementation_complexity": "SIGNIFICANTLY_DIFFICULT"}, "PLAN_COMPLEXITY"),
])
def test_visibly_hard_work_routes_to_the_strong_implementer(plan, reason):
    result = wp.assess_difficulty(plan)
    assert result["route"] == wp.ROUTE_STRONG and any(r.startswith(reason) for r in result["reasons"])


def test_a_legacy_plan_without_a_packet_key_keeps_the_old_routing():
    assert wp.assess_difficulty({"goal": "x"})["route"] == wp.ROUTE_DEFAULT


def test_tolerant_evidence_name_matching():
    assert wp.evidence_matches("unit tests", "Unit-tests (pytest)")
    assert wp.evidence_matches("unit tests", "unit")
    assert not wp.evidence_matches("unit tests", "lint")


# ── final self-audit and static sanity ─────────────────────────────────────

def test_self_audit_rows_have_stable_names_so_a_later_pass_supersedes_a_fail():
    first = {r["name"]: r["status"] for r in wp.audit_checks({"self_audit": audit_with(A3_SYNTAX="FAIL")})}
    second = {r["name"]: r["status"] for r in wp.audit_checks({"self_audit": ALL_PASS_AUDIT})}
    assert first["self_audit::A3_SYNTAX"] == "FAIL" and second["self_audit::A3_SYNTAX"] == "PASS"
    assert second["self_audit::completeness"] == "PASS"
    missing = wp.audit_checks({"summary": "done"})
    assert missing == [{"name": "self_audit::completeness", "status": "WARN",
                        "summary": "implementer returned no final self-audit"}]


def test_static_sanity_catches_the_simplest_defects(tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    (tmp_path / "bad.json").write_text('{"a": 1,}', encoding="utf-8")
    (tmp_path / "merge.txt").write_text("a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> x\n", encoding="utf-8")
    rows = {r["name"]: r for r in wp.static_sanity_checks(
        tmp_path, ["bad.py", "bad.json", "merge.txt"], claimed_files=["bad.py", "ghost.py"],
        planned_files=["bad.py"], expect_changes=True)}
    assert rows["static::python_syntax"]["status"] == "FAIL" and "bad.py:1" in rows["static::python_syntax"]["summary"]
    assert rows["static::json_syntax"]["status"] == "FAIL"
    assert rows["static::conflict_markers"]["status"] == "FAIL"
    assert rows["static::changes_present"]["status"] == "PASS"
    assert rows["static::reported_files_match"]["status"] == "WARN" and "ghost.py" in rows["static::reported_files_match"]["summary"]
    assert rows["static::within_work_packet"]["status"] == "WARN"
    (tmp_path / "bad.py").write_text("def f():\n    pass\n", encoding="utf-8")
    fixed = {r["name"]: r["status"] for r in wp.static_sanity_checks(tmp_path, ["bad.py"])}
    assert fixed["static::python_syntax"] == "PASS"


def test_done_with_no_change_is_a_failure_when_the_packet_lists_files():
    rows = {r["name"]: r["status"] for r in wp.static_sanity_checks(None, [], expect_changes=True)}
    assert rows == {"static::changes_present": "FAIL"}


# ── controller integration (scripted providers, real controller) ───────────

def packet_plan(refs, packet):
    def build(ctx):
        out = initial_plan(refs)(ctx)
        out["work_packet"] = packet
        return out
    return build


def policy_rig(tmp_path, packet, *, env=None):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "core export"}]
    h = Harness(tmp_path, mandate=m, env=env)
    h.roles = production_roles()
    h.defaults().script("plan", packet_plan(["A"], packet))
    h.scripts["execute"] = [{"summary": "implemented", "checks": ok_checks(), "changed_files": ["src/export/core.py"],
                             "self_audit": ALL_PASS_AUDIT}]
    return h


def journal(h, kind):
    return ctl.AutonomyJournal(h.stats / "RUN1/AUTONOMY/autonomy_events.jsonl", "RUN1").by_type(kind)


def profile_of(state, executor):
    return [e["profile"] for e in state["executions"] if e["executor"] == executor]


def test_clear_packet_goes_to_the_default_implementer(tmp_path):
    h = policy_rig(tmp_path, PACKET)
    state = h.controller().run()
    assert profile_of(state, "execute") == ["GPT6_LUNA_HIGH"]
    assessed = journal(h, "WORK_PACKET_ASSESSED")[0]["payload"]
    assert assessed["route"] == "DEFAULT" and assessed["present"] is True
    assert state["iterations"][0]["outcome"] == "PASS"


def test_visibly_hard_packet_goes_to_the_strong_implementer_up_front(tmp_path):
    h = policy_rig(tmp_path, dict(PACKET, needs_strong_implementer=True,
                                  strong_implementer_reason="concurrency between exporter and scheduler"))
    state = h.controller().run()
    execute = next(e for e in state["executions"] if e["executor"] == "execute")
    assert execute["profile"] == "SOL_6_1_MEDIUM"
    assert execute["selection"]["selection_reason"] == "WORK_PACKET_DIFFICULTY"


def test_failed_self_audit_is_repaired_before_any_reviewer_is_paid(tmp_path):
    h = policy_rig(tmp_path, PACKET)
    h.scripts["execute"] = [{"summary": "implemented", "checks": ok_checks(), "changed_files": ["src/export/core.py"],
                             "self_audit": audit_with(A2_VERIFIED="FAIL")}]
    h.script("repair", {"summary": "ran the verification", "checks": ok_checks(), "changed_files": [],
                        "addressed_findings": ["SELF_VERIFY::self_audit::A2_VERIFIED"], "uncertainties": [],
                        "self_audit": ALL_PASS_AUDIT})
    state = h.controller().run()
    calls = h.calls
    assert calls.index("repair") < calls.index("review")
    it = state["iterations"][0]
    assert it["repairs"][0]["addresses"] == ["SELF_VERIFY::self_audit::A2_VERIFIED"]
    assert it["evidence_state"]["self_audit::A2_VERIFIED"] == "PASS" and it["outcome"] == "PASS"
    assert journal(h, "IMPLEMENTER_SELF_AUDIT")[0]["payload"]["failing"] == ["self_audit::A2_VERIFIED"]


def test_strong_implementers_work_is_not_handed_to_a_weaker_repairer(tmp_path):
    h = policy_rig(tmp_path, dict(PACKET, needs_strong_implementer=True, strong_implementer_reason="hard"))
    h.scripts["execute"] = [{"summary": "implemented", "checks": ok_checks(), "changed_files": ["src/export/core.py"],
                             "self_audit": audit_with(A4_REFERENCES="FAIL")}]
    h.script("repair", {"summary": "fixed the import", "checks": ok_checks(), "self_audit": ALL_PASS_AUDIT})
    state = h.controller().run()
    assert profile_of(state, "repair") == ["SOL_6_1_MEDIUM"]


class DiskEnv(FakeEnv):
    def __init__(self, root):
        super().__init__()
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def changed_files(self):
        return ["src/export/core.py"]

    def describe(self):
        return {"worktree": str(self.root), "base_head": "base0"}


def test_syntax_error_is_caught_deterministically_and_repaired_before_review(tmp_path):
    env = DiskEnv(tmp_path / "wt")
    target = env.root / "src/export/core.py"
    target.parent.mkdir(parents=True)
    target.write_text("def export(:\n    return 1\n", encoding="utf-8")
    h = policy_rig(tmp_path, PACKET, env=env)

    def repair(ctx):
        target.write_text("def export():\n    return 1\n", encoding="utf-8")
        return {"summary": "fixed the signature", "checks": ok_checks(), "changed_files": ["src/export/core.py"],
                "self_audit": ALL_PASS_AUDIT}
    h.script("repair", repair)
    state = h.controller().run()
    it = state["iterations"][0]
    assert it["repairs"][0]["addresses"] == ["SELF_VERIFY::static::python_syntax"]
    assert h.calls.index("repair") < h.calls.index("review")
    assert it["evidence_state"]["static::python_syntax"] == "PASS" and it["outcome"] == "PASS"


def test_handoffs_carry_the_packet_and_the_audit_checklist(tmp_path):
    h = policy_rig(tmp_path, PACKET)
    h.controller().run()
    ctx = h.ctxs["execute"][0]
    handoff = aa.build_handoff("execute", ctx)
    assert handoff["WORK_PACKET"] == PACKET
    assert [row["id"] for row in handoff["AUDIT_CHECKLIST"]] == list(wp.AUDIT_IDS)
    assert "self_audit" in aa.OUTPUT_SCHEMAS["execute"]["required"]
    assert "work_packet" in aa.OUTPUT_SCHEMAS["plan"]["required"]
    assert "FINAL SELF-AUDIT" in aa.ROLE_INSTRUCTIONS["execute"] and "work_packet is MANDATORY" in aa.ROLE_INSTRUCTIONS["plan"]


def test_review_pretreatment_is_off_by_default():
    assert "prepare_packet" not in aa.build_direct_executors()
    assert "prepare_packet" in aa.build_direct_executors(review_pretreatment=True)


# ── Gemini CLI provider ─────────────────────────────────────────────────────

@pytest.fixture
def fake_gemini(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = ROOT / "aaw_autonomy_fake_cli.py"
    if os.name == "nt":
        (bin_dir / "gemini.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = bin_dir / "gemini"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    scenario = tmp_path / "scenario.json"
    monkeypatch.setenv("AAW_FAKE_CLI_SCENARIO", str(scenario))
    profiles = {p["profile_id"]: p for p in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
    cfg = {"allow_same_model_fresh_context": True,
           "roles": {r: {"profile_id": "GEMINI_3_1_PRO"} for r in ac.ROLES + tuple(ac.OPTIONAL_ROLE_ALIASES)}}
    roles = ac.validate_roles(cfg, profiles)

    def calls():
        path = scenario.with_name(scenario.name + ".calls.jsonl")
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
    return {"roles": roles, "write": lambda data: scenario.write_text(json.dumps(data), encoding="utf-8"),
            "calls": calls}


PLAN_OUT = {"status": "ITERATION", "mandate_hash": "$MANDATE_HASH", "goal": "add the export module",
            "roadmap_refs": ["A"], "scope_justification": "item A of the roadmap",
            "acceptance_criteria": ["$ITERATION_CRITERIA"], "touched_areas": ["src/export"],
            "decisions": [{"kind": "TESTS", "summary": "unit tests"}], "skipped_items": [], "reason": None}
IMPL_OUT = {"summary": "implemented", "changed_files": ["src/export/core.py"],
            "checks": [{"name": "unit", "status": "PASS", "summary": "1 passed"}], "deviations": [], "uncertainties": []}
VERIFY_OUT = {"summary": "verified", "checks": [{"name": "unit", "status": "PASS", "summary": "1 passed"}]}
PASS_OUT = {"verdict": "PASS", "summary": "meets criteria", "findings": []}


class DirEnv(FakeEnv):
    def __init__(self, path):
        super().__init__()
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def describe(self):
        return {"worktree": str(self.path), "base_head": "base0"}


def single_item_mandate():
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "core export"}]
    return m


def test_gemini_cli_runs_every_role_through_the_real_adapter_path(tmp_path, fake_gemini):
    fake_gemini["write"]({"PLANNER": [{"output": PLAN_OUT, "fenced": True}],
                          "IMPLEMENTER": [{"output": IMPL_OUT, "write_files": {"src/export/core.py": "x = 1\n"}}],
                          "SELF-VERIFIER": [{"output": VERIFY_OUT}], "REVIEWER": [{"output": PASS_OUT}],
                          "FINAL REVIEWER": [{"output": PASS_OUT}]})
    env = DirEnv(tmp_path / "wt")
    c = ctl.AutonomyController.start("RUN1", single_item_mandate(), executors=aa.build_direct_executors(timeout=60),
                                     env=env, roles=fake_gemini["roles"], stats_root=tmp_path / "stats")
    state = c.run()
    assert state["status"] == ac.AWAITING_HUMAN and state["hold"]["promotable"], state["escalation"]
    calls = fake_gemini["calls"]()
    assert [x["role"] for x in calls] == ["PLANNER", "IMPLEMENTER", "SELF-VERIFIER", "REVIEWER", "FINAL REVIEWER"]
    by_role = {x["role"]: x["argv"] for x in calls}
    for role, mode in (("PLANNER", "plan"), ("IMPLEMENTER", "yolo"), ("REVIEWER", "plan")):
        argv = by_role[role]
        assert argv[argv.index("--approval-mode") + 1] == mode
        assert "--skip-trust" in argv and argv[argv.index("--model") + 1] == "gemini-3.1-pro-preview"
        assert argv[argv.index("--output-format") + 1] == "json"
    assert all(e["provider_session_id"] for e in state["executions"])


def test_gemini_answer_missing_required_fields_is_rejected_not_trusted(tmp_path, fake_gemini):
    fake_gemini["write"]({"PLANNER": [{"output": PLAN_OUT}],
                          "IMPLEMENTER": [{"output": {"summary": "done"}}]})
    env = DirEnv(tmp_path / "wt")
    c = ctl.AutonomyController.start("RUN1", single_item_mandate(), executors=aa.build_direct_executors(timeout=60),
                                     env=env, roles=fake_gemini["roles"], stats_root=tmp_path / "stats")
    state = c.run()
    assert state["status"] == ac.AWAITING_HUMAN and state["escalation"]["code"] == ac.E_EXECUTOR


def test_gemini_parse_extracts_json_from_prose_and_fences():
    envelope = json.dumps({"session_id": "S1", "response": "ok:\n```json\n{\"a\": 1}\n```", "stats": {"x": 1}})
    session, usage, raw, meta = aa.DirectRoleExecutor._parse("gemini", 0, envelope, Path("unused"))
    assert (session, usage, raw) == ("S1", {"x": 1}, {"a": 1})
    assert aa.classify_failure(41, "", "")[0] == "AUTH"


def test_gemini_detection_reports_login_without_reading_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-value-not-read")
    spec = next(p for p in pp.PROVIDERS if p.provider_id == "gemini")
    row = pp.detect_provider(spec, runner=lambda argv, timeout: (0, "0.62.0\n", ""),
                             which=lambda harness: "/usr/bin/gemini")
    assert (row["status"], row["version"], row["login"]) == (pp.FOUND, "0.62.0", pp.LOGGED_IN)
    assert "GEMINI_API_KEY" in row["login_detail"] and "dummy" not in row["login_detail"]
    ids = {p["profile_id"] for p in row["profiles"]}
    assert {"GEMINI_3_1_PRO", "GEMINI_3_FLASH"} <= ids
    assert all(p["probe_required"] for p in row["profiles"] if p["profile_id"].startswith("GEMINI"))
    argv = pp.probe_argv("gemini", "gemini", "gemini-3-flash-preview")
    assert argv[argv.index("--approval-mode") + 1] == "plan" and "--prompt" in argv


def test_product_routing_offers_only_alternatives_this_machine_can_run():
    routing = {"profiles": {"GEMINI_3_1_PRO": {"available": False, "unavailable_reason": "default off"},
                            "GPT6_LUNA_HIGH": {}, "ANTIGRAVITY_FREE": {}}}
    out = pr._routing_for_machine(routing, {"GEMINI_3_1_PRO", "GPT6_LUNA_HIGH"})
    assert out["profiles"]["GEMINI_3_1_PRO"]["available"] is True
    assert "unavailable_reason" not in out["profiles"]["GEMINI_3_1_PRO"]
    assert out["profiles"]["ANTIGRAVITY_FREE"]["available"] is False
    assert routing["profiles"]["GEMINI_3_1_PRO"]["available"] is False      # the shipped block is not mutated
