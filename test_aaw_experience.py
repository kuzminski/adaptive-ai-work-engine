"""The passive experience engine: task kinds, rows from real runs, per-kind benchmark, optimum, horizon, live view."""

import copy
import os

import pytest

import aaw_experience as ex
import aaw_telemetry as tel
import autonomy_chain as chain
import autonomy_contract as ac
from test_autonomy import Harness, mandate_fixture, plan


# ── helpers ──────────────────────────────────────────────────────────────────

def real_run(tmp_path, goals, *, length=3, final=None, run_id="RUN1"):
    """A genuine controller run (scripted executors) written in the product's runs-root layout."""
    m = mandate_fixture(max_iterations=20)
    m["roadmap_mandate"]["items"] = [{"item_id": f"I{i}", "title": g} for i, g in enumerate(goals)]
    h = Harness(tmp_path, mandate=m).defaults()
    h.roles["chain"] = chain.normalize_config({"enabled": True, "length": length})
    h.script("plan", *[plan([f"I{i}"], goal=g) for i, g in enumerate(goals)])
    if final:
        h.scripts["final_review"] = list(final)
    c = h.controller(run_id)
    state = c.run()
    return h, c, state


def row(kind, profile, solved, cost, **over):
    base = {"schema": ex.ROW_SCHEMA, "run_id": "R", "iteration_id": f"I{id(over)}{cost}{profile}{solved}", "kind": kind,
            "profile_id": profile, "model": profile.lower(), "solved": solved, "difficulty": "EASY", "repairs": 0,
            "cost_usd": cost, "proxy_units": cost * 1000, "wall_s": 60.0, "total_cost_usd": cost * 2,
            "total_proxy_units": cost * 2000, "total_wall_s": 100.0}
    base.update(over)
    base["task_id"] = base.get("task_id") or f"{base['run_id']}:{base['iteration_id']}"
    return base


def many(kind, profile, n, solved_n, cost):
    return [row(kind, profile, i < solved_n, cost, iteration_id=f"{kind}{profile}{i}") for i in range(n)]


# ── task kind ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("goal,touched,kind", [
    ("Napraw błąd w paginacji ostatniej strony", ["shop/pagination.py"], "BUGFIX"),
    ("Fix the crash when the cart is empty", [], "BUGFIX"),
    ("Zrefaktoryzuj moduł notify i wydziel wspólny renderer", [], "REFACTOR"),
    ("Uzupełnij README i changelog", ["README.md"], "DOCS"),
    ("Popraw layout przycisku i style CSS na stronie głównej", ["web/app.css"], "UI"),
    ("Dodaj pipeline CI i obraz Docker", [".github/workflows/ci.yml"], "INFRA"),
    ("Zbadaj dlaczego zapytania są wolne i przygotuj raport", [], "ANALYSIS"),
    ("Dodaj eksport do CSV", ["shop/report.py"], "FEATURE"),
    ("Dodaj testy jednostkowe dla koszyka", ["tests/test_cart.py"], "TESTS"),
])
def test_classify_task_polish_and_english(goal, touched, kind):
    assert ex.classify_task(goal, touched=touched)["kind"] == kind


def test_classify_task_without_signal_is_other_and_uncertain():
    out = ex.classify_task("zrób to")
    assert out["kind"] == "OTHER" and out["confidence"] == 0.0


def test_path_hints_break_ties_but_goal_words_dominate():
    assert ex.classify_task("zmiana", touched=["docs/guide.md", "docs/api.md"])["kind"] == "DOCS"
    assert ex.classify_task("Napraw błąd", touched=["docs/guide.md"])["kind"] == "BUGFIX"


# ── rows from a real run ─────────────────────────────────────────────────────

def test_rows_describe_each_real_iteration(tmp_path):
    h, c, state = real_run(tmp_path, ["Napraw błąd A", "Dodaj eksport B", "Uzupełnij README C"])
    records = tel.read_records(c.dir / "telemetry.jsonl")
    rows = ex.rows_from_state(state, records, project="demo")
    assert [r["kind"] for r in rows] == ["BUGFIX", "FEATURE", "DOCS"]
    assert all(r["solved"] is True and r["profile_id"] and r["project"] == "demo" for r in rows)
    assert [r["review_mode"] for r in rows] == ["LIGHT", "LIGHT", "CHAIN_CLOSE"]
    assert all(r["calls"] >= 3 for r in rows)


def test_unclosed_and_escalated_work_is_neither_solved_nor_silently_counted(tmp_path):
    h, c, state = real_run(tmp_path, ["Napraw A", "Dodaj B", "Dodaj C"],
                           final=[{"verdict": "ESCALATE", "summary": "needs a product decision"}])
    rows = ex.rows_from_state(state, tel.read_records(c.dir / "telemetry.jsonl"))
    assert [r["solved"] for r in rows] == [None, None, False]       # two provisional, one escalated
    bench = ex.benchmark([{**r, "cost_usd": 1.0} for r in rows])
    assert bench["rows"] == 1                                       # only the decided iteration counts


def test_chain_close_review_cost_is_shared_by_the_whole_chain(tmp_path):
    h, c, state = real_run(tmp_path, ["Napraw A", "Dodaj B", "Dodaj C"])
    records = copy.deepcopy(tel.read_records(c.dir / "telemetry.jsonl"))
    for rec in records:
        rec["cost_usd"] = 1.0
        rec["proxy_units"] = 10.0
        rec["wall_s"] = 1.0
    close_reviews = [r for r in records if r["category"] == "REVIEW" and r["review_mode"] == "CHAIN_CLOSE"]
    assert len(close_reviews) == 2                                  # the closing REVIEW and FINAL_REVIEW
    rows = ex.rows_from_state(state, records)
    # iterations 1-2: plan?+execute+verify+light review = own calls + 2/3 of the close reviews
    own = lambda i: [r for r in records if r["iteration_id"] == state["iterations"][i]["iteration_id"]
                     and not (r["category"] == "REVIEW" and r["review_mode"] == "CHAIN_CLOSE")]
    for i in range(3):
        assert rows[i]["total_cost_usd"] == pytest.approx(len(own(i)) + 2 / 3)
    assert sum(r["total_cost_usd"] for r in rows) == pytest.approx(
        sum(r["cost_usd"] for r in records if r["iteration_id"] in {x["iteration_id"] for x in state["iterations"]}))


def test_unpriced_calls_leave_cost_unknown_instead_of_zero(tmp_path):
    h, c, state = real_run(tmp_path, ["Napraw A", "Dodaj B", "Dodaj C"])
    rows = ex.rows_from_state(state, tel.read_records(c.dir / "telemetry.jsonl"))
    assert all(r["cost_usd"] is None for r in rows)                  # scripted executors report no usage


def test_scan_reads_runs_and_caches_until_a_file_changes(tmp_path):
    real_run(tmp_path, ["Napraw A", "Dodaj B", "Dodaj C"], run_id="RUN_A1")
    real_run(tmp_path, ["Dodaj X", "Dodaj Y"], length=2, run_id="RUN_B1")
    runs_root = tmp_path / "stats"
    first = ex.scan(runs_root)
    assert len(first["summaries"]) == 2 and len(first["rows"]) == 5
    assert {s["outcome"] for s in first["summaries"]} == {"FINISHED"}
    again = ex.load_run(runs_root / "RUN_A1")
    assert again is ex.load_run(runs_root / "RUN_A1")                # cached object
    path = runs_root / "RUN_A1" / "AUTONOMY" / "telemetry.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    os.utime(path, ns=(10**18, 10**18))
    assert ex.load_run(runs_root / "RUN_A1") is not again


def test_a_broken_run_never_breaks_the_scan(tmp_path):
    real_run(tmp_path, ["Dodaj A", "Dodaj B"], length=2, run_id="RUN_OK1")
    broken = tmp_path / "stats" / "RUN_BAD1" / "AUTONOMY"
    broken.mkdir(parents=True)
    (broken / "autonomy_state.json").write_text("{not json", encoding="utf-8")
    assert len(ex.scan(tmp_path / "stats")["summaries"]) == 1


def test_run_summary_names_how_the_run_ended(tmp_path):
    _, c, state = real_run(tmp_path, ["Dodaj A", "Dodaj B", "Dodaj C"],
                           final=[{"verdict": "ESCALATE", "summary": "x"}])
    s = ex.run_summary(state, [], None, [])
    assert s["outcome"] == "ESCALATED" and s["why"] == ac.E_REVIEW
    assert ex.run_summary({**state, "escalation": None, "hold": {"reason": "ITERATION_CAP_REACHED"}}, [], None, [])["outcome"] == "CAP"
    assert ex.run_summary({**state, "status": "RUNNING"}, [], None, [])["outcome"] == "RUNNING"


# ── the benchmark ────────────────────────────────────────────────────────────

def test_benchmark_is_per_kind_with_pareto_best_and_confidence():
    rows = (many("BUGFIX", "CHEAP", 10, 9, 0.01) + many("BUGFIX", "PRICEY", 10, 10, 0.20)
            + many("BUGFIX", "NEW", 2, 2, 0.001) + many("DOCS", "CHEAP", 4, 4, 0.01))
    bench = ex.benchmark(rows)
    bug = bench["kinds"]["BUGFIX"]
    points = {p["profile_id"]: p for p in bug["points"]}
    assert bug["best"] == "CHEAP" and points["CHEAP"]["is_best"] and points["CHEAP"]["on_pareto"]
    assert points["PRICEY"]["on_pareto"]                          # best rate, so not dominated
    assert points["NEW"]["confidence"] == "TOO_FEW" and not points["NEW"]["is_best"]   # cheapest but only n=2
    assert points["CHEAP"]["confidence"] == "RELIABLE" and bench["kinds"]["DOCS"]["points"][0]["confidence"] == "PRELIMINARY"
    assert bench["basis"] == "usd" and bench["unit"] == "USD"
    assert bench["warnings"] and all(len(w) > 20 for w in bench["warnings"])      # whole sentences, not characters


def test_the_overall_optimum_follows_the_users_own_mix_of_work():
    rows = (many("UI", "A", 6, 6, 0.02) + many("UI", "B", 6, 6, 0.05)
            + many("ANALYSIS", "A", 6, 2, 0.02) + many("ANALYSIS", "B", 6, 6, 0.05))
    ui_person = ex.benchmark(rows, mix={"UI": 0.9, "ANALYSIS": 0.1})["overall"]
    research_person = ex.benchmark(rows, mix={"UI": 0.1, "ANALYSIS": 0.9})["overall"]
    assert ui_person[0]["profile_id"] == "A" and research_person[0]["profile_id"] == "B"
    assert ex.mix_of(rows) == {"UI": 0.5, "ANALYSIS": 0.5}


def test_the_overall_best_must_be_reliable_not_merely_cheap():
    rows = many("BUGFIX", "CHEAPBAD", 8, 3, 0.01) + many("BUGFIX", "SOLID", 8, 8, 0.05)
    overall = ex.benchmark(rows)["overall"]
    assert overall[0]["profile_id"] == "CHEAPBAD" and not overall[0]["is_best"]
    assert [o["profile_id"] for o in overall if o["is_best"]] == ["SOLID"]
    assert not any(o["is_best"] for o in ex.benchmark(many("BUGFIX", "ONLYBAD", 8, 3, 0.01))["overall"])


def test_overall_ignores_profiles_without_enough_coverage_of_the_mix():
    rows = many("UI", "A", 6, 6, 0.02) + many("ANALYSIS", "B", 6, 6, 0.05)
    overall = ex.benchmark(rows, mix={"UI": 0.9, "ANALYSIS": 0.1})["overall"]
    assert [o["profile_id"] for o in overall] == ["A"]


def test_total_scope_includes_review_overhead():
    rows = many("BUGFIX", "A", 4, 4, 0.01)
    impl = ex.benchmark(rows)["kinds"]["BUGFIX"]["points"][0]["cost_per_solved"]
    total = ex.benchmark(rows, scope="total")["kinds"]["BUGFIX"]["points"][0]["cost_per_solved"]
    assert total == pytest.approx(impl * 2)


def test_benchmark_falls_back_to_proxy_units_and_warns_when_prices_are_missing():
    rows = [{**r, "cost_usd": None} for r in many("BUGFIX", "A", 4, 4, 0.01) + many("BUGFIX", "B", 4, 4, 0.02)]
    bench = ex.benchmark(rows)
    assert bench["basis"] == "proxy" and bench["warnings"]


# ── what would be optimal ────────────────────────────────────────────────────

def test_recommendation_to_switch_names_the_saving_and_its_evidence():
    rows = many("BUGFIX", "OLD", 10, 9, 0.10) + many("BUGFIX", "NEW", 5, 5, 0.02)
    # the user mostly uses OLD, tried NEW a few times
    bench = ex.benchmark(rows, names={"OLD": "Model Stary", "NEW": "Model Nowy"})
    recs = ex.recommendations(bench, rows)
    switch = next(r for r in recs if r["type"] == "SWITCH")
    assert switch["from"] == "OLD" and switch["to"] == "NEW" and switch["saving"] > 0.7
    assert "Model Nowy" in switch["text"] and "Model Stary" in switch["text"]


def test_recommendation_keeps_a_profile_that_is_already_best_and_collects_when_data_is_thin():
    rows = many("BUGFIX", "A", 10, 10, 0.02) + many("BUGFIX", "B", 4, 4, 0.10) + many("DOCS", "A", 2, 2, 0.01)
    recs = ex.recommendations(ex.benchmark(rows), rows)
    assert {"KEEP": "BUGFIX", "COLLECT": "DOCS"} == {r["type"]: r["kind"] for r in recs if r["type"] in ("KEEP", "COLLECT")}


def test_no_switch_is_suggested_when_the_cheaper_profile_is_clearly_less_reliable():
    rows = many("BUGFIX", "SAFE", 10, 10, 0.10) + many("BUGFIX", "CHEAPBAD", 5, 3, 0.01)
    assert not [r for r in ex.recommendations(ex.benchmark(rows), rows) if r["type"] == "SWITCH"]


def test_review_share_and_chain_health_tips():
    rows = many("BUGFIX", "A", 4, 4, 0.01)
    records = [{"category": "REVIEW", "proxy_units": 80.0, "executor": "review", "tokens": {}} for _ in range(8)] + \
              [{"category": "IMPLEMENTATION", "proxy_units": 20.0, "executor": "execute", "tokens": {}} for _ in range(4)]
    recs = ex.recommendations(ex.benchmark(rows), rows, records)
    assert any(r["type"] == "REVIEW_SHARE" for r in recs)
    closes = [row("BUGFIX", "A", True, .01, review_mode="CHAIN_CLOSE", repairs=0, iteration_id=f"c{i}") for i in range(4)]
    assert any(r["type"] == "LONGER_CHAINS" for r in ex.recommendations(ex.benchmark(closes), closes))
    bad = [row("BUGFIX", "A", True, .01, review_mode="CHAIN_CLOSE", repairs=1, iteration_id=f"d{i}") for i in range(4)]
    assert any(r["type"] == "SHORTER_CHAINS" for r in ex.recommendations(ex.benchmark(bad), bad))


def test_settlement_reports_a_cautious_range_only_where_the_benchmark_supports_it():
    history = many("BUGFIX", "OLD", 10, 9, 0.10) + many("BUGFIX", "NEW", 5, 5, 0.02)
    bench = ex.benchmark(history)
    mine = [row("BUGFIX", "OLD", True, 0.10, iteration_id=f"m{i}", run_id="MINE") for i in range(3)]
    s = ex.settlement(mine, bench)
    assert s["actual"] == pytest.approx(0.30) and s["items"][0]["to"] == "NEW"
    assert 0 < s["saving"]["low"] < s["saving"]["high"] < s["actual"]
    assert ex.settlement([row("DOCS", "OLD", True, 0.1)], bench)["saving"] is None


# ── horizon and forecast ─────────────────────────────────────────────────────

def summary(outcome, iterations, why=None):
    return {"outcome": outcome, "iterations": iterations, "why": why}


def test_horizon_refuses_to_speak_without_enough_finished_runs():
    out = ex.horizon([summary("FINISHED", 5), summary("RUNNING", 2)])
    assert out["enough"] is False and "Za mało" in out["text"]


def test_horizon_reports_how_far_runs_get():
    runs = [summary("FINISHED", 8), summary("FINISHED", 5), summary("FINISHED", 12), summary("ESCALATED", 3, "REVIEW_ESCALATED"),
            summary("CAP", 40)]
    out = ex.horizon(runs)
    assert out["enough"] and out["finished_share"] == 0.6 and out["outcomes"]["ESCALATED"] == 1
    assert out["iterations"]["median"] == 8 and out["stop_reasons"] == {"REVIEW_ESCALATED": 1}
    assert out["by_size"]["1-3"]["finished_share"] == 0.0 and out["by_size"]["9+"]["runs"] == 2


def running_state(pending=3, standing=False):
    roadmap = {f"P{i}": {"status": "PENDING"} for i in range(pending)}
    roadmap["DONE1"] = {"status": "DONE"}
    if standing:
        roadmap["CONTINUE"] = {"status": "PENDING", "recurring": True}
    return {"status": "RUNNING", "phase": "EXECUTE", "roadmap": roadmap, "chain_state": {"chain_id": 1, "closed_chains": 0},
            "iterations": [], "deferred_findings": []}


def test_forecast_from_this_run_after_two_iterations():
    own = [row("FEATURE", "A", True, 1.0, total_cost_usd=2.0, total_wall_s=100.0, iteration_id=f"x{i}") for i in range(2)]
    own[1]["total_cost_usd"] = 4.0
    out = ex.forecast(running_state(pending=3), own, [])
    est = out["estimate"]
    assert est["source"] == "TEN PRZEBIEG" and est["iterations_left"] == 3 and est["unit"] == "USD"
    assert est["cost"]["low"] <= est["cost"]["mid"] <= est["cost"]["high"] and est["cost"]["mid"] == pytest.approx(9.0)


def test_forecast_falls_back_to_history_then_to_an_honest_nothing():
    own = [row("FEATURE", "A", None, 1.0, iteration_id="y0", total_cost_usd=2.0)]
    history = many("FEATURE", "A", 5, 5, 1.0)
    assert ex.forecast(running_state(), own, history)["estimate"]["source"] == "HISTORIA"
    nothing = ex.forecast(running_state(), own, [])
    assert nothing["estimate"] is None and "Za mało danych" in nothing["note"]


def test_forecast_says_when_the_end_is_open():
    assert "kontynuuj" in ex.forecast(running_state(standing=True), [], many("FEATURE", "A", 5, 5, 1.0))["note"]


def test_forecast_for_a_new_goal_uses_the_users_history_of_that_kind():
    history = many("BUGFIX", "A", 10, 9, 0.02) + many("DOCS", "A", 5, 5, 0.01)
    bench = ex.benchmark(history, names={"A": "Model A"})
    out = ex.forecast_for_goal("Napraw błąd w koszyku", [], bench, history)
    assert out["kind"] == "BUGFIX" and out["recommended"]["label"] == "Model A" and out["per_iteration"]["mid"] > 0
    first = ex.forecast_for_goal("Zbadaj wydajność", [], bench, history)
    assert first["recommended"] is None and "zbierze dane sam" in first["note"]
    unpriced = [{**r, "total_cost_usd": None, "total_proxy_units": None} for r in many("BUGFIX", "A", 2, 2, 0.02)]
    thin = ex.forecast_for_goal("Napraw błąd", [], ex.benchmark(unpriced), unpriced)
    assert thin["n"] == 2 and "Masz już 2 iteracji" in thin["note"] and "pierwsze zadania" not in thin["note"]
    priced_in_proxy = [{**r, "total_cost_usd": None} for r in many("BUGFIX", "A", 5, 5, 0.02)]
    assert ex.forecast_for_goal("Napraw błąd", [], ex.benchmark(priced_in_proxy), priced_in_proxy)["unit"] == "proxy"


# ── the live view ────────────────────────────────────────────────────────────

def test_journey_follows_a_run_from_idea_to_the_gate(tmp_path):
    _, _, state = real_run(tmp_path, ["Dodaj A", "Dodaj B", "Dodaj C"])
    stages = {s["id"]: s["state"] for s in ex.journey(state)}
    assert stages == {"idea": "done", "charter": "done", "chains": "done", "close": "done", "polish": "pending", "gate": "active"}
    mid = copy.deepcopy(state)
    mid.update(status="RUNNING", phase="EXECUTE")
    mid["chain_state"] = {"chain_id": 1, "closed_chains": 0}
    mid["iterations"][-1]["status"] = "IN_PROGRESS"
    stages = {s["id"]: s["state"] for s in ex.journey(mid)}
    assert stages["chains"] == "active" and stages["close"] == "pending" and stages["gate"] == "pending"
    closing = copy.deepcopy(mid)
    closing["phase"] = "FINAL_REVIEW"
    assert {s["id"]: s["state"] for s in ex.journey(closing)}["close"] == "active"


def test_journey_shows_where_an_escalated_run_stopped(tmp_path):
    _, _, state = real_run(tmp_path, ["Dodaj A", "Dodaj B", "Dodaj C"],
                           final=[{"verdict": "ESCALATE", "summary": "needs a product decision"}])
    stages = {s["id"]: s["state"] for s in ex.journey(state)}
    assert stages["close"] == "failed" and stages["gate"] == "active" and stages["polish"] == "pending"
    early = copy.deepcopy(state)
    early["iterations"][-1]["chain"]["review_mode"] = "LIGHT"
    early["escalation"]["from_phase"] = "EXECUTE"
    assert {s["id"]: s["state"] for s in ex.journey(early)}["chains"] == "failed"


def test_chain_slots_show_provisional_work_and_the_closing_review_slot(tmp_path):
    _, c, state = real_run(tmp_path, ["Dodaj A", "Dodaj B", "Dodaj C", "Dodaj D"], length=3)
    mid = copy.deepcopy(state)
    mid.update(status="RUNNING", phase="EXECUTE", chain_state={"chain_id": 2, "closed_chains": 1})
    rows = ex.rows_from_state(mid, tel.read_records(c.dir / "telemetry.jsonl"))
    slots = ex.chain_slots(mid, rows)
    assert slots["chain_id"] == 2 and slots["length"] == 3 and slots["closed"] == 1
    assert [s["state"] for s in slots["slots"]][0] in ("accepted", "provisional", "active")
    assert slots["slots"][-1]["closing"] and slots["slots"][1]["state"] == "pending"


def test_live_view_assembles_every_panel(tmp_path):
    _, c, state = real_run(tmp_path, ["Dodaj A", "Dodaj B", "Dodaj C"])
    records = tel.read_records(c.dir / "telemetry.jsonl")
    rows = ex.rows_from_state(state, records)
    live = ex.live_view(state, c.journal.read(), records, rows, [])
    assert {"journey", "chain", "meter", "feed", "forecast", "now", "deferred_open"} <= set(live)
    assert live["meter"]["calls"] == len(records) and live["feed"][0]["text"]
    assert any(f["event"] == "CHAIN_ACCEPTED" for f in live["feed"])
    assert live["forecast"]["estimate"] is None                     # nothing to base an estimate on: says so


# ── the demo preview ─────────────────────────────────────────────────────────

def test_demo_rows_are_deterministic_labelled_and_persona_dependent():
    a, b = ex.demo_rows("backend"), ex.demo_rows("backend")
    assert a == b and all(r["source"] == "DEMO" for r in a)
    bench = ex.benchmark(a)
    assert bench["kinds"] and bench["overall"]
    for persona in ex.PERSONAS:                      # every persona must give a chart with several models to compare
        assert len(ex.benchmark(ex.demo_rows(persona))["overall"]) >= 3, persona
    web_kinds = set(ex.benchmark(ex.demo_rows("web"))["kinds"])
    assert "UI" in web_kinds and ex.mix_of(ex.demo_rows("web")) != ex.mix_of(a)
