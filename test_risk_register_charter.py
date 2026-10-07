"""Human-confirmed risks → minimum risk floors in the planner's frozen charter.

A risk typed in the wizard (or accepted from the idea intake) is stored in
`roadmap_mandate.risk_register`; its severity sets implementation / final-review
floors the initial architect may raise but never lower or drop, and the risks
for an iteration's roadmap items reach the implementer and the reviewers.
"""

from __future__ import annotations

import copy
import json

import pytest

import autonomy_adapters as aa
import autonomy_contract as ac
import autonomy_controller as ctl
import product_runs as prun
import product_view as pv
from test_autonomy import mandate_fixture
from test_autonomy_policy_v0_3 import initial_plan, policy_harness, prepared
from test_product_mvp import env, form, settled, wait_for  # noqa: F401  (pytest fixture)


def register_mandate(register):
    m = mandate_fixture()
    m["roadmap_mandate"]["risk_register"] = register
    return ac.validate_mandate(m)


def risk(risk_id, severity, items=(), text="risk text"):
    return {"risk_id": risk_id, "description": text, "severity": severity, "item_ids": list(items)}


def architect_charter(mandate, risk_guidance):
    return {**{k: v for k, v in ac.directional_charter_template(mandate).items() if k != "risk_guidance"},
            "risk_guidance": risk_guidance}


# ── contract ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("row, message", [
    (risk("R1", "SEVERE"), "severity"),
    (risk("R1", "HIGH", ["Z"]), "item_ids"),
    ({**risk("R1", "HIGH"), "description": "  "}, "description"),
    ({**risk("R1", "HIGH"), "mitigation": 5}, "mitigation"),
    ({"description": "x", "severity": "LOW"}, "risk_id"),
])
def test_invalid_register_rows_are_refused(row, message):
    with pytest.raises(ac.AutonomyError, match=message):
        register_mandate([row])


def test_duplicate_risk_ids_are_refused():
    with pytest.raises(ac.AutonomyError, match="duplicate risk_id"):
        register_mandate([risk("R1", "LOW"), risk("R1", "HIGH")])


def test_severity_maps_to_floors_scoped_and_whole_run():
    m = register_mandate([risk("R1", "HIGH", ["A"], "data loss on save"), risk("R2", "MEDIUM", ["B"]),
                          risk("R3", "LOW"), risk("R4", "CRITICAL", ["C"])])
    floors = {row["item_id"]: row for row in ac.mandated_risk_floors(m)}
    assert [row["item_id"] for row in ac.mandated_risk_floors(m)] == ["A", "B", "C"]   # roadmap order
    assert (floors["A"]["implementation_floor"], floors["A"]["final_review_floor"]) == ("HARDER", "HARD")
    assert (floors["B"]["implementation_floor"], floors["B"]["final_review_floor"]) == ("NORMAL", "HARD")
    assert (floors["C"]["implementation_floor"], floors["C"]["final_review_floor"]) == \
        ("SIGNIFICANTLY_DIFFICULT", "CRITICAL")
    assert floors["A"]["reason"] == "HUMAN_CONFIRMED_RISK R1 (HIGH): data loss on save"
    assert "R3" not in json.dumps(floors)            # LOW never raises a floor


def test_a_whole_run_risk_applies_to_every_item_and_the_maximum_wins():
    m = register_mandate([risk("R1", "MEDIUM"), risk("R2", "HIGH", ["B"])])
    floors = {row["item_id"]: row for row in ac.mandated_risk_floors(m)}
    assert set(floors) == {"A", "B", "C"}
    assert floors["B"]["implementation_floor"] == "HARDER" and "R1" in floors["B"]["reason"]
    assert floors["C"]["implementation_floor"] == "NORMAL" and floors["C"]["final_review_floor"] == "HARD"
    assert [r["risk_id"] for r in ac.risks_for_items(m, ["B"])] == ["R1", "R2"]
    assert [r["risk_id"] for r in ac.risks_for_items(m, ["A"])] == ["R1"]
    assert [r["risk_id"] for r in ac.risks_for_items(m, [])] == ["R1"]


def test_without_a_register_the_template_and_charter_keep_their_previous_shape():
    m = ac.validate_mandate(mandate_fixture())
    template = ac.directional_charter_template(m)
    assert "risk_guidance" not in template
    charter = ac.validate_directional_charter({**template, "risk_guidance": []}, m)
    assert set(charter) == {"mandate_hash", "objective", "roadmap_items", "acceptance_criteria", "boundaries",
                            "human_gate_conditions", "risk_guidance", "charter_hash"}
    assert charter["charter_hash"] == ac.canonical_hash({k: v for k, v in charter.items() if k != "charter_hash"})


def test_template_prefills_floors_and_a_verbatim_copy_needs_no_adjustment():
    m = register_mandate([risk("R1", "HIGH", ["A"])])
    template = ac.directional_charter_template(m)
    assert template["risk_guidance"] == ac.mandated_risk_floors(m)
    charter = ac.validate_directional_charter(copy.deepcopy(template), m)
    assert charter["risk_guidance"] == template["risk_guidance"]
    assert charter["mandated_risk_floors"] == template["risk_guidance"]
    assert charter["risk_floor_adjustments"] == []


def test_an_architect_cannot_drop_a_mandated_floor():
    m = register_mandate([risk("R1", "CRITICAL", ["B"])])
    charter = ac.validate_directional_charter(architect_charter(m, []), m)
    assert charter["risk_guidance"] == ac.mandated_risk_floors(m)
    assert charter["risk_floor_adjustments"] == [{"item_id": "B", "action": "ADDED",
                                                  "implementation_floor": "SIGNIFICANTLY_DIFFICULT",
                                                  "final_review_floor": "CRITICAL"}]


def test_an_architect_cannot_lower_a_floor_but_may_raise_one():
    m = register_mandate([risk("R1", "HIGH", ["A"]), risk("R2", "MEDIUM", ["C"])])
    rows = [{"item_id": "A", "implementation_floor": "NORMAL", "final_review_floor": "CRITICAL",
             "reason": "touches persistence"},
            {"item_id": "C", "implementation_floor": "SIGNIFICANTLY_DIFFICULT", "final_review_floor": "CRITICAL",
             "reason": "docs generator rewrite"}]
    charter = ac.validate_directional_charter(architect_charter(m, rows), m)
    by_item = {row["item_id"]: row for row in charter["risk_guidance"]}
    assert by_item["A"]["implementation_floor"] == "HARDER"           # raised back to the human floor
    assert by_item["A"]["final_review_floor"] == "CRITICAL"           # the architect's higher floor stays
    assert by_item["A"]["reason"] == "touches persistence | HUMAN_CONFIRMED_RISK R1 (HIGH): risk text"
    assert by_item["C"]["implementation_floor"] == "SIGNIFICANTLY_DIFFICULT"
    assert charter["risk_floor_adjustments"] == [{"item_id": "A", "action": "RAISED", "fields": {
        "implementation_floor": {"architect": "NORMAL", "enforced": "HARDER"}}}]


# ── controller: the floors drive model selection ────────────────────────────

def test_mandated_floor_routes_implementation_and_final_review_even_if_the_architect_ignores_it(tmp_path):
    m = mandate_fixture()
    m["roadmap_mandate"]["items"] = [{"item_id": "A", "title": "architecture"}]
    m["roadmap_mandate"]["risk_register"] = [risk("R1", "HIGH", ["A"], "cross-module dependency risk")]
    h = prepared(policy_harness(tmp_path, mandate=m))
    h.script("plan", initial_plan(["A"]))           # the architect returns risk_guidance: []
    state = h.controller(with_prep=True).run()
    execute = next(row for row in state["executions"] if row["executor"] == "execute")
    final = next(row for row in state["executions"] if row["executor"] == "final_review")
    assert execute["profile"] == "GPT6_LUNA_VERY_HIGH"
    assert final["profile"] == "SONNET_5_5_MEDIUM"
    assert final["selection"]["selection_reason"] == "DIRECTIONAL_CHARTER_RISK_FLOOR"
    assert any("HUMAN_CONFIRMED_RISK R1" in e for e in final["selection"]["complexity_risk_evidence"])
    events = ctl.AutonomyJournal(h.stats / "RUN1" / "AUTONOMY" / "autonomy_events.jsonl", "RUN1").read()
    frozen = next(e for e in events if e["event_type"] == "DIRECTIONAL_CHARTER_FROZEN")["payload"]
    assert frozen["mandated_risk_floors"] == 1
    assert frozen["risk_floor_adjustments"][0]["action"] == "ADDED"
    assert frozen["risk_guidance"] == [{"item_id": "A", "implementation_floor": "HARDER",
                                        "final_review_floor": "HARD"}]
    # the risk reaches the implementer and both reviewers
    assert [r["risk_id"] for r in h.ctxs["execute"][0]["mandate"]["roadmap_mandate"]["risk_register"]] == ["R1"]


# ── handoffs ────────────────────────────────────────────────────────────────

class _Env:
    def describe(self):
        return {"worktree": "W"}


def _ctx(mandate, **extra):
    return {"mandate": mandate, "role": "implementer", "env": _Env(),
            "execution": {"run_id": "R", "iteration_id": "I", "execution_id": "E", "descriptor_path": "d"}, **extra}


def test_handoffs_carry_the_relevant_risks_only():
    m = register_mandate([risk("R1", "HIGH", ["A"]), risk("R2", "LOW", ["C"])])
    plan = {"goal": "g", "roadmap_refs": ["A"], "work_packet": None}
    initial = aa.build_handoff("plan", _ctx(m, role="initial_planner", iteration_index=1,
                                            planning_stage="INITIAL_ARCHITECT", roadmap={}, history=[]))
    assert initial["DIRECTIONAL_CHARTER_TEMPLATE"]["risk_guidance"][0]["item_id"] == "A"
    execute = aa.build_handoff("execute", _ctx(m, plan=plan, iteration={}))
    assert [r["risk_id"] for r in execute["RISK_FOCUS"]] == ["R1"]
    repair = aa.build_handoff("repair", _ctx(m, plan=plan, iteration={}, findings=[], attempt=2,
                                             repair_packet={"x": 1}))
    assert [r["risk_id"] for r in repair["RISK_FOCUS"]] == ["R1"]       # kept in the compact repair packet
    review = aa.build_handoff("review", _ctx(m, iteration={"plan": plan}, review_kind="REVIEW", packet={},
                                             raw={}))
    assert [r["risk_id"] for r in review["RISK_CHECKS"]] == ["R1"]
    other = aa.build_handoff("execute", _ctx(m, plan={**plan, "roadmap_refs": ["B"]}, iteration={}))
    assert "RISK_FOCUS" not in other
    plain = ac.validate_mandate(mandate_fixture())
    assert "RISK_FOCUS" not in aa.build_handoff("execute", _ctx(plain, plan=plan, iteration={}))
    assert "RISK_CHECKS" not in aa.build_handoff("final_review", _ctx(plain, iteration={"plan": plan},
                                                                      review_kind="FINAL", packet={}, raw={}))


def test_role_instructions_name_the_risk_contract():
    assert "never lower or drop it" in aa.ROLE_INSTRUCTIONS["plan"]
    assert "risk_register" in aa.ROLE_INSTRUCTIONS["plan"] and "pitfalls" in aa.ROLE_INSTRUCTIONS["plan"]
    assert "RISK_FOCUS" in aa.ROLE_INSTRUCTIONS["execute"]
    assert "RISK_CHECKS" in aa.ROLE_INSTRUCTIONS["review"] and "RISK_CHECKS" in aa.ROLE_INSTRUCTIONS["final_review"]


# ── product: wizard text / intake rows → register → summary ────────────────

def test_wizard_lines_parse_level_scope_and_defaults():
    rows = prun.normalize_risks("[wysokie] utrata danych (punkty: pierwsza, 3)\n"
                                "- przecinki w polach CSV\n"
                                "[CRITICAL] wyciek kluczy API\n\n"
                                "[niskie] wolny eksport (punkt 2; 1)", direction_count=2)
    assert [(r["risk_id"], r["severity"], r["item_ids"]) for r in rows] == [
        ("R1", "HIGH", ["STEP_1", "STEP_3"]), ("R2", "MEDIUM", []), ("R3", "CRITICAL", []),
        ("R4", "LOW", ["STEP_2", "STEP_1"])]
    assert rows[0]["description"] == "utrata danych" and rows[1]["description"] == "przecinki w polach CSV"


@pytest.mark.parametrize("text, message", [
    ("[groźne] coś", "Nieznany poziom"),
    ("ryzyko (punkty: 4)", "ma 3 punktów"),
    ("ryzyko (punkty: 0)", "ma 3 punktów"),
    ("ryzyko (punkty: drugi)", "Nie rozumiem"),
])
def test_wizard_lines_with_mistakes_are_refused_in_plain_polish(text, message):
    with pytest.raises(prun.ProductError, match=message):
        prun.normalize_risks(text, direction_count=2)


def test_intake_rows_keep_their_ids_scope_and_mitigation():
    rows = prun.normalize_risks([
        {"risk_id": "RZ1", "description": "migracja schematu", "severity": "high", "points": [2],
         "mitigation": "kopia zapasowa", "source": "intake"},
        {"risk_id": "RZ1", "text": "duplikat id", "level": "krytyczne", "item_ids": ["STEP_1"]},
    ], direction_count=1)
    assert rows[0] == {"risk_id": "RZ1", "description": "migracja schematu", "severity": "HIGH",
                       "item_ids": ["STEP_2"], "mitigation": "kopia zapasowa", "source": "INTAKE"}
    assert rows[1]["risk_id"] == "R2" and rows[1]["severity"] == "CRITICAL" and rows[1]["item_ids"] == ["STEP_1"]


def test_mandate_without_risks_is_unchanged_and_with_risks_validates():
    base = {"goal": "Aplikacja wydatków", "first_iteration": "model danych", "directions": "- eksport\n- raport"}
    plain = prun.build_mandate(prun.normalize_form(base), "M", {})
    assert "risk_register" not in plain["roadmap_mandate"]
    with_risks = prun.build_mandate(prun.normalize_form({**base, "advanced": {"risks": "[wysokie] zapis (punkty: 2)"}}),
                                    "M", {})
    frozen = ac.validate_mandate(with_risks)
    assert frozen["roadmap_mandate"]["risk_register"][0]["item_ids"] == ["STEP_2"]
    summary = prun.risk_summary(frozen)
    assert summary["risks"][0]["points"] == ["punkt 2"] and summary["risks"][0]["level"] == "wysokie"
    assert summary["floors"] == [{"item_id": "STEP_2", "point": "punkt 2", "implementation_floor": "HARDER",
                                  "implementation": "trudniejsza", "final_review_floor": "HARD",
                                  "final_review": "mocny"}]


def test_preview_shows_risks_floors_and_warns_on_critical(env):  # noqa: F811
    env.install("claude")
    preview = prun.preview_task(form(env, advanced={"risks": "[krytyczne] wyciek sekretów\n[niskie] literówki"}),
                                detection=prun.detection_snapshot(refresh=True))
    assert preview["can_start"], preview["blockers"]
    assert [r["risk_id"] for r in preview["risks"]] == ["R1", "R2"]
    assert {f["point"] for f in preview["risk_floors"]} == {"punkt 1 (pierwsza iteracja)", "punkt 2",
                                                            "dalsza praca autonomiczna"}
    assert any("Ryzyko krytyczne (R1)" in w for w in preview["warnings"])
    assert prun.preview_task(form(env), detection=prun.detection_snapshot())["risks"] == []


def test_product_run_freezes_the_human_floor_and_hands_risks_to_every_role(env):  # noqa: F811
    env.install("claude")
    run_id = prun.start_task(form(env, advanced={"risks": "[wysokie] zła obsługa pustego imienia (punkty: pierwsza)"}),
                             detection=prun.detection_snapshot(refresh=True))["run_id"]
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE, view
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text(encoding="utf-8"))
    charter = state["directional_charter"]
    assert charter["risk_floor_adjustments"][0] == {"item_id": "STEP_1", "action": "ADDED",
                                                    "implementation_floor": "HARDER", "final_review_floor": "HARD"}
    first = state["iterations"][0]
    final = next(e for e in state["executions"]
                 if e["executor"] == "final_review" and e.get("iteration_id") == first["iteration_id"])
    assert any("HUMAN_CONFIRMED_RISK R1" in e for e in final["selection"]["complexity_risk_evidence"])
    charter_brief = view["timeline"][0]["briefs"][0]
    assert any("Progi z Twoich ryzyk: 1" in line and "STEP_1 (dodano)" in line for line in charter_brief["done"])
    calls = [c for c in env.calls() if c.get("iteration_id") == first["iteration_id"]]
    keys = {c["role"]: set(c.get("handoff_keys") or []) for c in calls}
    assert "RISK_FOCUS" in keys["IMPLEMENTER"]
    assert "RISK_CHECKS" in keys["REVIEWER"] and "RISK_CHECKS" in keys["FINAL REVIEWER"]
