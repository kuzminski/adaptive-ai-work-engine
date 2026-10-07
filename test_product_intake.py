"""Idea intake, roadmap forecast, the recommended-model proposal and opt-in exploration setup (no provider, no worker)."""

import json
from pathlib import Path

import pytest

import aaw_experience as ex
import autonomy_contract as ac
import product_home
import product_intake as pi
import product_runs as prun
import product_view as pv
from test_aaw_experience import many, row


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AAW_PRODUCT_HOME", str(tmp_path / "aaw_home"))
    ex._CACHE.clear()
    return tmp_path


def raw_proposal(**over):
    base = {"title": "Podział rachunków", "goal": "Aplikacja do dzielenia wydatków ze znajomymi.",
            "first_iteration": "Model wydatku i obliczenie, kto komu ile oddaje, z testami.",
            "roadmap": [
                {"title": "Napraw zaokrąglanie groszy w rozliczeniu", "why": "dokładność", "kind": "BUGFIX", "size": "S",
                 "human_required": False, "human_reason": None},
                {"title": "Dodaj eksport rozliczenia do CSV", "why": "udostępnianie", "kind": "FEATURE", "size": "M",
                 "human_required": False, "human_reason": None},
                {"title": "Podłącz płatności online", "why": "spłacanie długów", "kind": "INFRA", "size": "L",
                 "human_required": True, "human_reason": "wymaga konta u operatora płatności"}],
            "acceptance_criteria": ["rozliczenie sumuje się do zera"], "constraints": ["bez zewnętrznych zależności"],
            "forbidden_areas": [".env"], "required_evidence": ["unit tests"], "assumptions": ["waluta: PLN"],
            "open_questions": ["Czy potrzebne konta użytkowników?"], "risks": [{"severity": "HIGH", "text": "zaokrąglenia"}],
            "done_definition": "Można rozliczyć wyjazd i wyeksportować wynik."}
    base.update(over)
    return base


def runner_returning(raw, usage=None):
    calls = []

    def run(prompt, schema, cwd):
        calls.append({"prompt": prompt, "schema": schema, "cwd": cwd})
        return {"raw": raw, "usage": usage or {"input_tokens": 20_000, "output_tokens": 2_000}, "meta": {}, "wall_s": 41.0, "rc": 0,
                "runtime": {"profile_id": "CLAUDE_OPUS_5_5_HIGH", "model": "claude-opus-5-5", "effort": "high", "harness": "claude"}}
    run.calls = calls
    return run


def history_rows(n=10):
    rows = many("BUGFIX", "A", n, n, 0.02) + many("FEATURE", "A", n, n, 0.05)
    return {"rows": rows, "summaries": []}


# ── normalisation ────────────────────────────────────────────────────────────

def test_normalize_cleans_and_bounds_the_planners_output():
    out = pi.normalize(raw_proposal(roadmap=[{"title": "x" * 900, "kind": "NOPE", "size": "XL", "human_required": True,
                                              "human_reason": "decyzja"}] + [{"title": f"punkt {i}"} for i in range(60)]))
    assert len(out["roadmap"]) == pi.MAX_ITEMS and len(out["roadmap"][0]["title"]) == 400
    assert out["roadmap"][0]["size"] == "M" and out["roadmap"][0]["kind"] in ex.KINDS and out["roadmap"][0]["human_required"]
    assert out["roadmap"][1]["human_reason"] is None and out["roadmap"][1]["human_required"] is False


@pytest.mark.parametrize("raw", [None, "text", {}, {"goal": "ok goal", "roadmap": [], "first_iteration": ""}])
def test_normalize_refuses_unusable_output_with_a_human_message(raw):
    with pytest.raises(pi.IntakeError):
        pi.normalize(raw)


def test_prefill_marks_human_only_items_so_the_product_makes_them_human_required():
    p = pi.normalize(raw_proposal())
    prefill = pi.to_prefill(p)
    assert prefill["directions"].splitlines() == ["- Napraw zaokrąglanie groszy w rozliczeniu", "- Dodaj eksport rozliczenia do CSV",
                                                  "- [człowiek] Podłącz płatności online"]
    assert prefill["advanced"]["required_evidence"] == "unit tests" and prefill["advanced"]["forbidden_areas"] == ".env"
    assert prefill["advanced"]["risks"] == [{"description": "zaokrąglenia", "severity": "HIGH", "item_ids": [], "source": "INTAKE"}]
    form = prun.normalize_form({"goal": prefill["goal"], "first_iteration": prefill["first_iteration"], "directions": prefill["directions"]})
    mandate = prun.build_mandate(form, "M1", {})
    ac.validate_mandate(mandate)                                    # the engine accepts it
    gated = [i for i in mandate["roadmap_mandate"]["items"] if i.get("human_required")]
    assert [g["title"] for g in gated] == ["Podłącz płatności online"]
    assert all(not i.get("human_required") for i in mandate["roadmap_mandate"]["items"] if i["item_id"] in ("STEP_1", "STEP_2", "STEP_3"))


def test_intake_risks_reach_the_mandate_register_and_set_charter_floors():
    p = pi.normalize(raw_proposal(risks=[{"severity": "HIGH", "text": "błędy zaokrągleń"}, {"severity": "LOW", "text": "drobiazg"}]))
    pf = pi.to_prefill(p)
    form = prun.normalize_form({"goal": pf["goal"], "first_iteration": pf["first_iteration"], "directions": pf["directions"],
                                "advanced": pf["advanced"]})
    register = form["advanced"]["risks"]
    assert [(r["severity"], r["source"], r["item_ids"]) for r in register] == [("HIGH", "INTAKE", []), ("LOW", "INTAKE", [])]
    mandate = prun.build_mandate(form, "M1", {})
    mandate = ac.validate_mandate(mandate)
    assert [r["description"] for r in mandate["roadmap_mandate"]["risk_register"]] == ["błędy zaokrągleń", "drobiazg"]
    floors = ac.mandated_risk_floors(mandate)
    assert floors and all(f["implementation_floor"] == "HARDER" and f["final_review_floor"] == "HARD" for f in floors)   # LOW adds none


def test_the_human_marker_is_case_and_language_tolerant():
    assert prun.HUMAN_GATE.match("[Człowiek] x") and prun.HUMAN_GATE.match("[HUMAN] x") and not prun.HUMAN_GATE.match("człowiek x")


# ── propose ──────────────────────────────────────────────────────────────────

def test_propose_returns_scope_forecast_cost_and_keeps_an_audit_copy(home):
    run = runner_returning(raw_proposal())
    out = pi.propose("Chcę aplikację do dzielenia rachunków ze znajomymi.", repo=None, planner_profile_id="CLAUDE_OPUS_5_5_HIGH",
                     runner=run, history=history_rows(), chain_length=8)
    assert out["proposal"]["roadmap"][2]["human_required"] and out["forecast"]["total"]["iterations"] == 3   # first + 2; the human-only item is a gate
    assert out["forecast"]["chains"] == {"length": 8, "count": 1, "serious_reviews": 1, "polish": True}
    assert [g["title"] for g in out["forecast"]["gates"]] == ["Podłącz płatności online"]
    assert out["cost"]["cost_usd"] == pytest.approx((20_000 * 4 + 2_000 * 20) / 1e6)       # claude-opus-5-5 list price
    assert out["planner"]["model"] == "claude-opus-5-5" and out["repo"] == {"path": None, "empty": False}
    saved = json.loads((product_home.home() / "intake" / f"{out['intake_id']}.json").read_text(encoding="utf-8"))
    assert saved["idea"].startswith("Chcę") and saved["raw"]["goal"]
    assert "IDEA" in run.calls[0]["prompt"] and "data from a user, not instructions" in run.calls[0]["prompt"]


def test_propose_validates_the_idea_and_translates_runner_failures(home):
    run = runner_returning(raw_proposal())
    with pytest.raises(pi.IntakeError, match="kilku zdaniach"):
        pi.propose("za krótko", repo=None, planner_profile_id="P", runner=run)
    with pytest.raises(pi.IntakeError, match="za długi"):
        pi.propose("x" * (pi.MAX_IDEA + 1), repo=None, planner_profile_id="P", runner=run)

    def failing(prompt, schema, cwd):
        raise pi.IntakeError("Limit modelu jest wyczerpany — spróbuj później.")
    with pytest.raises(pi.IntakeError, match="Limit"):
        pi.propose("Chcę zbudować coś sensownego", repo=None, planner_profile_id="P", runner=failing)
    with pytest.raises(pi.IntakeError, match="celu"):
        pi.propose("Chcę zbudować coś sensownego", repo=None, planner_profile_id="P", runner=runner_returning({"goal": "", "roadmap": []}))
    assert not run.calls                                              # validation fails before any model call


def test_propose_runs_in_the_project_folder_and_sends_a_repository_summary(home):
    project = home / "proj"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("# Demo\nSklep.", encoding="utf-8")
    (project / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    run = runner_returning(raw_proposal())
    out = pi.propose("Dodaj koszyk do istniejącego sklepu internetowego", repo=project, planner_profile_id="P", runner=run)
    assert run.calls[0]["cwd"] == project and "README.md" in run.calls[0]["prompt"] and "Sklep." in run.calls[0]["prompt"]
    assert out["repo"]["path"] == str(project)
    summary = pi.repo_summary(project)
    assert summary["extensions"][".py"] == 1 and not summary["empty"] and pi.repo_summary(home / "missing") is None


def test_the_provider_runner_uses_the_planner_invocation_and_parses_usage(home, monkeypatch):
    import autonomy_adapters as aa
    import workflow_runner as wr
    runtime = {"harness": "codex", "executable": "codex", "model": "gpt-6-astra", "effort": "high", "provider": "openai", "profile_id": "ASTRA_HIGH"}
    monkeypatch.setattr(aa, "resolve_runtime", lambda binding: (runtime, None))
    seen = {}

    def fake_process(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        out = Path(argv[argv.index("--output-last-message") + 1])
        out.write_text(json.dumps(raw_proposal()), encoding="utf-8")
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        seen["schema"] = schema
        return 0, json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 50}}) + "\n", ""

    monkeypatch.setattr(wr, "run_process", fake_process)
    result = pi.provider_runner("ASTRA_HIGH")("prompt", pi.INTAKE_SCHEMA, home)
    assert "read-only" in seen["argv"] and seen["kwargs"]["dispatch"] is False and seen["kwargs"]["stdin"] == "prompt"
    assert set(seen["schema"]["required"]) == set(seen["schema"]["properties"])          # strict structured output
    assert result["usage"]["input_tokens"] == 1000 and result["raw"]["title"] and result["runtime"]["model"] == "gpt-6-astra"


def test_the_provider_runner_explains_an_unavailable_model_and_a_failed_call(home, monkeypatch):
    import autonomy_adapters as aa
    import workflow_runner as wr
    monkeypatch.setattr(aa, "resolve_runtime", lambda binding: (None, "CLI not logged in"))
    with pytest.raises(pi.IntakeError, match="not logged in"):
        pi.provider_runner("P")("prompt", pi.INTAKE_SCHEMA, home)
    runtime = {"harness": "codex", "executable": "codex", "model": "m", "effort": "high", "provider": "openai", "profile_id": "P"}
    monkeypatch.setattr(aa, "resolve_runtime", lambda binding: (runtime, None))
    monkeypatch.setattr(wr, "run_process", lambda argv, **kw: (1, "", "429 rate limit reached"))
    with pytest.raises(pi.IntakeError, match="Limit modelu"):
        pi.provider_runner("P")("prompt", pi.INTAKE_SCHEMA, home)


# ── roadmap forecast ─────────────────────────────────────────────────────────

def test_roadmap_forecast_sums_per_item_ranges_and_describes_chains_gates_and_reach():
    hist = history_rows()["rows"]
    items = [{"title": "Napraw błąd A"}, {"title": "Dodaj funkcję B"}, {"title": "Zrób coś człowieczego", "human_required": True}]
    horizon = ex.horizon([{"outcome": "FINISHED", "iterations": 4, "why": None}] * 3 + [{"outcome": "ESCALATED", "iterations": 5, "why": "X"}])
    out = ex.forecast_for_roadmap(items, hist, horizon_stats=horizon, chain_length=2, max_iterations=40)
    assert out["total"]["iterations"] == 2 and out["total"]["human_items"] == 1 and out["total"]["unit"] == "USD"
    assert out["chains"]["count"] == 1 and out["items"][2]["cost"] is None
    bug, feat = out["items"][0], out["items"][1]
    assert bug["source"] == "KIND" and feat["source"] == "KIND" and bug["cost"]["mid"] == pytest.approx(0.04)
    assert out["total"]["cost"]["low"] <= out["total"]["cost"]["mid"] <= out["total"]["cost"]["high"]
    assert out["gates"][0]["title"] == "Zrób coś człowieczego" and out["reach"]["enough"] and out["reach"]["finished_share"] == 0.75
    assert "75%" in out["reach"]["text"]


def test_roadmap_forecast_is_honest_without_history_and_warns_about_the_fuse():
    out = ex.forecast_for_roadmap([{"title": f"punkt {i}"} for i in range(5)], [], max_iterations=3, continuous=True)
    assert out["total"]["cost"] is None and out["items"][0]["source"] == "NONE" and out["reach"]["enough"] is False
    assert any("bezpiecznik" in w for w in out["warnings"]) and any("0 z 5" in w for w in out["warnings"]) and out["open_ended"]


def test_roadmap_forecast_keeps_items_with_the_same_title_apart():
    hist = many("BUGFIX", "A", 5, 5, 0.02) + many("FEATURE", "A", 5, 5, 0.10)
    out = ex.forecast_for_roadmap([{"title": "to samo", "kind": "BUGFIX"}, {"title": "to samo", "kind": "FEATURE"}], hist)
    assert [round(i["cost"]["mid"], 3) for i in out["items"]] == [0.04, 0.2]


# ── recommended model proposal ───────────────────────────────────────────────

def seed_history(home):
    from test_product_experience import seed_run
    seed_run("AAW_TASK_INTAKE01", ["Napraw błąd A", "Napraw błąd B", "Napraw błąd C"])
    seed_run("AAW_TASK_INTAKE02", ["Napraw błąd D", "Napraw błąd E", "Napraw błąd F"])


def test_forecast_view_proposes_a_model_with_the_facts_the_button_needs(home):
    from unittest import mock
    history = many("BUGFIX", "GPT6_LUNA_HIGH", 8, 8, 0.02)
    with mock.patch.object(pv, "_history", return_value={"rows": history, "summaries": [], "records": []}),             mock.patch.object(pv.prun, "detection_snapshot", return_value={}),             mock.patch.object(pv.prun.pp, "runnable_profiles", return_value={"GPT6_LUNA_HIGH", "TERRA_HIGH"}):
        out = pv.forecast_view({"goal": "Napraw błąd w eksporcie", "directions": ["Dodaj filtr", "[człowiek] Zdecyduj o logo"],
                                "current_profile_id": "TERRA_HIGH"})
        same = pv.forecast_view({"goal": "Napraw błąd w eksporcie", "directions": [], "current_profile_id": "GPT6_LUNA_HIGH"})
    assert out["kind"] == "BUGFIX" and out["roadmap"]["total"]["iterations"] == 2 and out["roadmap"]["gates"][0]["title"] == "Zdecyduj o logo"
    apply = out["recommended"]["apply"]
    assert apply == {"slot": "implementer_default", "profile_id": "GPT6_LUNA_HIGH", "runnable": True, "differs": True,
                     "current_profile_id": "TERRA_HIGH", "current_label": apply["current_label"], "kind": "BUGFIX",
                     "mode": "SLOT"}
    assert same["recommended"]["apply"]["differs"] is False             # already the model that will be used: no button


def test_with_an_implementer_chain_the_recommendation_becomes_step_one_and_keeps_the_escalation(home):
    from unittest import mock
    history = many("BUGFIX", "TERRA_HIGH", 8, 8, 0.02)
    chain = ["GPT6_LUNA_HIGH", "TERRA_HIGH", "GPT6_LUNA_MAX"]
    with mock.patch.object(pv, "_history", return_value={"rows": history, "summaries": [], "records": []}), \
            mock.patch.object(pv.prun, "detection_snapshot", return_value={}), \
            mock.patch.object(pv.prun.pp, "runnable_profiles", return_value=set(chain)):
        out = pv.forecast_view({"goal": "Napraw błąd", "directions": [], "current_profile_id": "GPT6_LUNA_HIGH", "current_chain": chain})
    apply = out["recommended"]["apply"]
    assert apply["mode"] == "CHAIN" and apply["slot"] == "implementer_chain" and apply["differs"] is True
    assert apply["chain"] == ["TERRA_HIGH", "GPT6_LUNA_HIGH", "GPT6_LUNA_MAX"]       # no duplicate, escalation kept in order
    assert apply["current_chain"] == chain


def test_the_audit_record_keeps_the_applied_chain():
    form = prun.normalize_form({"goal": "Zbuduj coś ciekawego", "advanced": {"recommendation": {
        "slot": "implementer_chain", "profile_id": "TERRA_HIGH", "chain": ["TERRA_HIGH", "GPT6_LUNA_HIGH"], "kind": "BUGFIX"}}})
    assert form["advanced"]["recommendation"]["chain"] == ["TERRA_HIGH", "GPT6_LUNA_HIGH"]
    assert form["advanced"]["recommendation"]["slot"] == "implementer_chain"


def test_a_recommended_profile_that_cannot_run_here_is_flagged_not_hidden():
    from unittest import mock
    history = many("BUGFIX", "NOT_HERE", 6, 6, 0.01)
    bench = ex.benchmark(history)
    with mock.patch.object(pv, "_history", return_value={"rows": history, "summaries": [], "records": []}), \
            mock.patch.object(pv.prun, "detection_snapshot", return_value={}), \
            mock.patch.object(pv.prun.pp, "runnable_profiles", return_value={"SOMETHING_ELSE"}):
        out = pv.forecast_view({"goal": "Napraw błąd", "directions": [], "current_profile_id": "SOMETHING_ELSE"})
    assert bench["kinds"]["BUGFIX"]["best"] == "NOT_HERE"
    assert out["recommended"]["apply"]["runnable"] is False and out["recommended"]["apply"]["differs"] is True


def test_the_applied_recommendation_is_recorded_as_a_user_confirmed_audit_entry():
    form = prun.normalize_form({"goal": "Zbuduj coś ciekawego", "advanced": {
        "profile_overrides": {"implementer_default": "TERRA_HIGH"},
        "recommendation": {"slot": "implementer_default", "profile_id": "TERRA_HIGH", "kind": "BUGFIX", "n": 9, "rate": 0.9,
                           "cost_per_solved": 0.02, "evil": "ignored", "previous_profile_id": "GPT6_LUNA_HIGH"}}})
    rec = form["advanced"]["recommendation"]
    assert rec == {"source": "EXPERIENCE", "slot": "implementer_default", "chain": None, "profile_id": "TERRA_HIGH", "kind": "BUGFIX",
                   "previous_profile_id": "GPT6_LUNA_HIGH", "n": 9, "rate": 0.9, "cost_per_solved": 0.02, "confirmed_by_user": True}
    assert form["advanced"]["profile_overrides"] == {"implementer_default": "TERRA_HIGH"}
    assert prun.normalize_form({"goal": "Zbuduj coś ciekawego"})["advanced"]["recommendation"] is None


# ── exploration plan and setup ───────────────────────────────────────────────

def test_exploration_plan_lists_thin_cells_thinnest_first_and_skips_known_failures():
    rows = (many("BUGFIX", "A", 1, 1, 0.01) + many("BUGFIX", "B", 3, 3, 0.01) + many("DOCS", "C", 6, 1, 0.01))
    plan = ex.exploration_plan(rows, ["A", "B", "C", "D"], exclude=("INFRA",))
    assert plan["wanted"]["BUGFIX"] == ["D", "A"] and "B" not in plan["wanted"]["BUGFIX"]       # B has 3: enough
    assert "C" in plan["skipped_poor"] and all("C" not in v for v in plan["wanted"].values())  # C: 1 of 6 solved
    assert "INFRA" not in plan["wanted"] and {"kind": "BUGFIX", "profile_id": "A", "n": 1} in plan["cells"]


def resolution_with(default="GPT6_LUNA_HIGH", cost="MEDIUM"):
    return {"slots": {"implementer_default": {"profile_id": default, "cost_class": cost}}}


def settings_with(**over):
    return {**product_home.DEFAULT_SETTINGS, **over}


def form_with(exploration=None):
    return {"advanced": {"exploration": exploration}}


def test_exploration_is_off_by_default_and_says_why(home, monkeypatch):
    info, cfg = prun.exploration_setup(form_with(), settings_with(), resolution_with(), {})
    assert cfg is None and info["enabled"] is False and info["reason"] == "wyłączona"


def test_exploration_candidates_are_runnable_recommended_and_no_more_expensive(home, monkeypatch):
    monkeypatch.setattr(prun.pp, "runnable_profiles", lambda d: {"GPT6_LUNA_HIGH", "GPT6_LUNA_VERY_HIGH", "TERRA_HIGH", "SONNET_HIGH", "OPUS_HIGH"})
    info, cfg = prun.exploration_setup(form_with(True), settings_with(), resolution_with(), {}, rows=[])
    ids = {c["profile_id"] for c in info["candidates"]}
    assert "GPT6_LUNA_HIGH" not in ids                                      # the default itself
    assert "OPUS_HIGH" not in ids                                           # HIGH cost class: dearer than the default
    assert {"TERRA_HIGH"} <= ids and cfg["enabled"] and cfg["max_percent"] == 20 and cfg["max_per_run"] == 3
    assert set(cfg["candidates"]) == set(cfg["wanted"]["BUGFIX"]) and "INFRA" not in cfg["wanted"]
    assert cfg["exclude_kinds"] == ["INFRA"] and info["thin_cells"]
    import autonomy_explore as explore
    assert explore.normalize_config(cfg)["enabled"]                         # what the controller will freeze is valid


def test_the_per_run_switch_overrides_the_setting_and_a_lone_default_means_nothing_to_explore(home, monkeypatch):
    monkeypatch.setattr(prun.pp, "runnable_profiles", lambda d: {"GPT6_LUNA_HIGH"})
    info, cfg = prun.exploration_setup(form_with(True), settings_with(exploration_enabled=False), resolution_with(), {}, rows=[])
    assert cfg is None and "brak innego" in info["reason"] and info["requested"] is True
    info, cfg = prun.exploration_setup(form_with(False), settings_with(exploration_enabled=True), resolution_with(), {}, rows=[])
    assert cfg is None and info["requested"] is False


def test_a_fully_measured_benchmark_leaves_nothing_to_explore(home, monkeypatch):
    monkeypatch.setattr(prun.pp, "runnable_profiles", lambda d: {"GPT6_LUNA_HIGH", "TERRA_HIGH"})
    rows = []
    for kind in ex.KINDS:
        rows += many(kind, "TERRA_HIGH", 3, 3, 0.01)
    info, cfg = prun.exploration_setup(form_with(True), settings_with(), resolution_with(), {}, rows=rows)
    assert cfg is None and "dość prób" in info["reason"]


def test_exploration_settings_are_validated(home):
    saved = product_home.save_settings({"exploration_enabled": True, "exploration_max_percent": 10, "exploration_max_per_run": 2})
    assert saved["exploration_enabled"] is True and saved["exploration_max_percent"] == 10
    for bad in ({"exploration_max_percent": 0}, {"exploration_max_percent": 90}, {"exploration_max_per_run": 0},
                {"exploration_enabled": "yes"}):
        with pytest.raises(ValueError):
            product_home.save_settings(bad)


def test_experience_view_reports_exploration_state_and_routes_are_registered(home):
    import inspect
    import product_server
    data = pv.experience_view({})
    assert "exploration" in data
    assert '"/api/intake/propose"' in inspect.getsource(product_server.make_handler)
