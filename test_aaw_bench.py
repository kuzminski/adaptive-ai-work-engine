"""AAW-Bench: task soundness, the production-controller runner (with a simulated model), and the analyzer math."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "BENCH"))

import aaw_bench as bench  # noqa: E402
import aaw_telemetry as tel  # noqa: E402
import autonomy_controller as ctl  # noqa: E402


# ── the tasks themselves ─────────────────────────────────────────────────────

def test_every_task_fails_on_the_start_repo_and_passes_with_its_reference_solution():
    report = bench.validate_tasks()
    assert len(report) >= 9
    assert [r["id"] for r in report if not r["ok"]] == []


def test_task_set_covers_all_three_difficulty_bands_and_declares_forbidden_paths_consistently():
    tasks = bench.load_tasks()
    assert {t["difficulty"] for t in tasks} == set(bench.DIFFICULTIES)
    for task in tasks:
        assert task["acceptance_criteria"] and (Path(task["dir"]) / "accept_test.py").is_file()
        assert (Path(task["dir"]) / "solution").is_dir()
        for forbidden in task.get("forbidden_paths", []):
            assert (Path(task["dir"]) / "repo" / forbidden).exists()


# ── the runner through the production controller, with a simulated model ─────

def simulated_model(task, *, solve=True, also_touch=None, tokens=None):
    usage = tokens or {"input_tokens": 300_000, "cached_input_tokens": 270_000, "cache_write_input_tokens": 0,
                       "output_tokens": 4_000, "reasoning_output_tokens": 1_000}

    def factory():
        def execute(ctx):
            worktree = Path(ctx["env"].describe()["worktree"])
            if solve:
                bench.overlay(Path(task["dir"]) / "solution", worktree)
            for rel in also_touch or []:
                (worktree / rel).write_text("# tampered\n", encoding="utf-8")
            ctl._write_once(Path(ctx["execution"]["result_path"]), {
                "execution_id": ctx["execution"]["execution_id"], "role": ctx["role"], "executor": "execute",
                "result": {}, "usage": usage, "wall_time_s": 12.5})
            return {"summary": "done", "changed_files": [], "checks": [
                {"name": "unit tests", "status": "PASS", "summary": "ok"}], "deviations": [], "uncertainties": []}
        return execute
    return factory


def task_named(prefix):
    return bench.load_tasks([prefix])[0]


def test_a_solving_model_scores_solved_with_measured_tokens_and_a_priced_cost(tmp_path):
    task = task_named("T01")
    row = bench.run_one(task, "GPT6_LUNA_HIGH", 1, workroot=tmp_path, execute_factory=simulated_model(task))
    assert row["solved"] and row["reason"] == "OK" and row["calls"] == 1
    assert row["tokens"]["input_total"] == 300_000 and row["tokens"]["output"] == 4_000
    assert row["model"] == "gpt-6-luna" and row["effort"] == "high"
    # (30k fresh * 0.10 + 270k cached * 0.01 + 4k out * 0.50) / 1e6
    assert row["cost_usd"] == pytest.approx(0.00770, abs=1e-6) and row["cost_basis"] == ["ESTIMATED"]
    assert row["proxy_units"] == tel.proxy_units({"input_total": 300_000, "input_cached": 270_000, "output": 4_000})


def test_a_model_that_changes_nothing_scores_acceptance_failed(tmp_path):
    task = task_named("T04")
    row = bench.run_one(task, "GPT6_LUNA_HIGH", 1, workroot=tmp_path, execute_factory=simulated_model(task, solve=False))
    assert row["solved"] is False and row["reason"] == "ACCEPTANCE_FAILED" and row["cost_usd"] is not None


def test_touching_a_forbidden_path_fails_even_when_the_tests_pass(tmp_path):
    task = task_named("T06")
    row = bench.run_one(task, "GPT6_LUNA_HIGH", 1, workroot=tmp_path,
                        execute_factory=simulated_model(task, also_touch=["shop/templates.py"]))
    assert row["solved"] is False and row["reason"] == "FORBIDDEN_PATH"


def test_an_executor_failure_is_a_failed_trial_not_a_crash(tmp_path):
    task = task_named("T01")

    def factory():
        def execute(ctx):
            raise ctl.ExecutorFailure("provider exited", dispatched=True)
        return execute

    row = bench.run_one(task, "GPT6_LUNA_HIGH", 1, workroot=tmp_path, execute_factory=factory)
    assert row["solved"] is False and row["reason"].startswith("EXECUTOR_")


def test_resume_key_set_skips_finished_trials(tmp_path):
    results = tmp_path / "r.jsonl"
    results.write_text(json.dumps({"schema": bench.ROW_SCHEMA, "task_id": "T01", "profile_id": "P", "repeat": 1}) + "\n"
                       + "not json\n", encoding="utf-8")
    assert bench.done_keys(results) == {("T01", "P", 1)}


def test_live_run_requires_an_explicit_spend_flag(tmp_path, capsys):
    code = bench.main(["run", "--profiles", "GPT6_LUNA_HIGH", "--tasks", "T01", "--results", str(tmp_path / "x.jsonl")])
    assert code == 0 and not (tmp_path / "x.jsonl").exists()
    assert "--yes-spend" in capsys.readouterr().out


# ── analyzer math ────────────────────────────────────────────────────────────

def row(task, profile, solved, cost, difficulty="EASY", repeat=1, wall=10.0):
    return {"schema": bench.ROW_SCHEMA, "task_id": task, "difficulty": difficulty, "profile_id": profile,
            "repeat": repeat, "solved": solved, "cost_usd": cost, "proxy_units": cost * 100, "wall_s": wall}


def test_wilson_interval_behaves():
    assert bench.wilson(0, 0) == (0.0, 1.0)
    lo, hi = bench.wilson(8, 10)
    assert 0.49 < lo < 0.5 and 0.94 < hi < 0.96
    assert bench.wilson(10, 10)[1] == 1.0 and bench.wilson(0, 10)[0] == 0.0


def test_cost_per_solved_divides_all_spend_by_successes():
    rows = [row("a", "P", True, 1.0), row("b", "P", False, 3.0), row("c", "P", True, 2.0)]
    s = bench.profile_stats(rows, "usd")["P"]
    assert s["n"] == 3 and s["solved"] == 2 and s["cost_per_solved"] == 3.0 and s["cost_mean"] == 2.0


def test_pareto_drops_dominated_profiles():
    rows = ([row(f"t{i}", "cheap_good", i < 9, 1.0) for i in range(10)]
            + [row(f"t{i}", "pricey_same", i < 9, 4.0) for i in range(10)]
            + [row(f"t{i}", "pricey_best", True, 5.0) for i in range(10)]
            + [row(f"t{i}", "cheap_bad", i < 3, 0.1) for i in range(10)]          # 0.33 per solved: cheapest, 30% rate
            + [row(f"t{i}", "worse_than_good", i < 3, 0.5) for i in range(10)])   # 1.67 per solved at 30%: dominated
    front = bench.pareto(bench.profile_stats(rows, "usd"))
    assert not {"pricey_same", "worse_than_good"} & set(front) and {"cheap_good", "pricey_best", "cheap_bad"} <= set(front)


def test_cascade_expected_cost_is_computed_per_task():
    rows = [row("A", "cheap", True, 1.0), row("B", "cheap", False, 1.0),
            row("A", "strong", True, 4.0), row("B", "strong", True, 4.0)]
    result = bench.cascade(rows, ["cheap", "strong"], "usd")
    assert result["success_rate"] == 1.0 and result["cost_per_task"] == 3.0       # A: 1, B: 1 + 4
    assert bench.cascade(rows, ["strong"], "usd")["cost_per_task"] == 4.0
    assert bench.cascade(rows, ["cheap"], "usd")["success_rate"] == 0.5
    assert bench.cascade(rows, ["cheap", "ghost"], "usd") is None                  # no data for a rung: no guess


def test_cascade_uses_measured_solve_rates_for_repeated_trials():
    rows = [row("A", "p", True, 2.0, repeat=1), row("A", "p", False, 2.0, repeat=2),
            row("A", "q", True, 6.0)]
    r = bench.cascade(rows, ["p", "q"], "usd")
    assert r["success_rate"] == 1.0 and r["cost_per_task"] == 5.0                   # 2 + 0.5 * 6


def test_best_ladders_respect_the_success_floor_and_prefer_the_cheaper_ladder():
    rows = [row("A", "cheap", True, 1.0), row("B", "cheap", False, 1.0),
            row("A", "strong", True, 4.0), row("B", "strong", True, 4.0)]
    best = bench.best_ladders(rows, "usd", floor=0.9)
    assert best[0]["ladder"] == ["cheap", "strong"] and best[0]["cost_per_task"] == 3.0
    assert all(b["success_rate"] >= 0.9 for b in best) and ["cheap"] not in [b["ladder"] for b in best]


def test_tier_recommendation_needs_enough_trials_and_clears_the_floor():
    rows = ([row(f"e{i}", "cheap", i != 0, 1.0) for i in range(5)] + [row(f"e{i}", "strong", True, 3.0) for i in range(5)]
            + [row(f"h{i}", "cheap", False, 1.0, "HARD") for i in range(5)] + [row(f"h{i}", "strong", i < 4, 3.0, "HARD")
                                                                               for i in range(5)]
            + [row("m0", "cheap", True, 1.0, "MEDIUM")])
    tiers = bench.tier_recommendations(rows, "usd", floor=0.8, min_n=3)
    assert tiers["EASY"]["profile_id"] == "cheap" and tiers["EASY"]["tier"] == "NORMAL"     # 4/5 = 0.8 clears the floor
    assert tiers["HARD"]["profile_id"] == "strong" and tiers["HARD"]["slot"] == "implementer_hard"
    assert tiers["MEDIUM"]["status"] == "INSUFFICIENT_DATA" and tiers["MEDIUM"]["profile_id"] is None
    none = bench.tier_recommendations([row(f"x{i}", "p", False, 1.0) for i in range(4)], "usd", floor=0.8, min_n=3)
    assert none["EASY"]["status"] == "NO_PROFILE_MEETS_FLOOR"


def test_basis_falls_back_to_proxy_when_any_row_is_unpriced_and_warns_across_models():
    rows = [row("a", "P", True, 1.0), {**row("b", "Q", True, 1.0), "cost_usd": None}]
    assert bench.choose_basis(rows) == "proxy" and bench.choose_basis(rows[:1]) == "usd"
    rows[0]["model"], rows[1]["model"] = "m1", "m2"
    report = bench.analyze(rows)
    assert report["basis"] == "proxy" and any("price-aware" in w for w in report["warnings"])
    assert "AAW-Bench analysis" in bench.render_analysis(report)


def test_rows_from_run_charge_only_implementation_work_and_map_complexity_to_difficulty():
    state = {"iterations": [
        {"iteration_id": "I1", "status": "ACCEPTED", "outcome": "PASS", "repair_attempts": 1,
         "plan": {"implementation_complexity": "HARDER"}},
        {"iteration_id": "I2", "status": "ESCALATED", "outcome": "ESCALATE", "repair_attempts": 0, "plan": {}}]}
    rec = lambda it, ex, cat, usd, units: {"iteration_id": it, "executor": ex, "category": cat, "profile_id": "P", "model": "m",
                                           "cost_usd": usd, "proxy_units": units, "wall_s": 5.0}
    records = [rec("I1", "execute", "IMPLEMENTATION", 1.0, 100), rec("I1", "repair", "IMPLEMENTATION", 0.5, 50),
               rec("I1", "review", "REVIEW", 9.0, 900), rec("I2", "execute", "IMPLEMENTATION", 2.0, 200)]
    rows = bench.rows_from_run(state, records)
    assert [(r["difficulty"], r["solved"], r["cost_usd"], r["calls"]) for r in rows] == [
        ("MEDIUM", True, 1.5, 2), ("EASY", False, 2.0, 1)]
