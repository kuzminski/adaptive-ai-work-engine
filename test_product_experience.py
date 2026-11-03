"""The product's experience surfaces over real run directories (no worker process, no provider)."""

import json

import pytest

import aaw_experience as ex
import autonomy_chain as chain
import product_home
import product_runs as prun
import product_view as pv
from test_autonomy import Harness, mandate_fixture, plan


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AAW_PRODUCT_HOME", str(tmp_path / "aaw_home"))
    ex._CACHE.clear()
    return tmp_path


def seed_run(run_id, goals, *, length=3, final=None, project="demo"):
    """A genuine controller run written where the product reads runs (runs_root/<run_id>/AUTONOMY + PRODUCT/task.json)."""
    m = mandate_fixture(max_iterations=20)
    m["roadmap_mandate"]["items"] = [{"item_id": f"I{i}", "title": g} for i, g in enumerate(goals)]
    h = Harness(product_home.home().parent, mandate=m).defaults()
    h.stats = prun.runs_root()
    h.roles["chain"] = chain.normalize_config({"enabled": True, "length": length})
    h.script("plan", *[plan([f"I{i}"], goal=g) for i, g in enumerate(goals)])
    if final:
        h.scripts["final_review"] = list(final)
    c = h.controller(run_id)
    c.run()
    product_home.write_json(prun.product_dir(run_id) / "task.json", {
        "form": {"goal": goals[0]}, "created_at": "2026-10-07T10:00:00+02:00",
        "workspace": {"project_name": project, "repo": "R", "worktree": "W"}, "resolution": {}})
    return c


def test_run_view_carries_the_live_block_and_a_settlement_at_the_gate(home):
    seed_run("AAW_TASK_TEST01", ["Napraw błąd A", "Dodaj B", "Dodaj C"])
    view = pv.run_view("AAW_TASK_TEST01")
    live = view["live"]
    assert {"journey", "chain", "meter", "feed", "forecast"} <= set(live)
    assert {s["id"]: s["state"] for s in live["journey"]}["gate"] == "active"
    assert live["chain"]["length"] == 3 and live["meter"]["calls"] > 0
    assert view["process"]["eta"] is None                      # no data to estimate from: still honest
    assert view["gate"]["settlement"]["saving"] is None and view["gate"]["settlement"]["unit"] in ("USD", "proxy")


def test_experience_view_builds_itself_from_the_runs_without_any_setup(home):
    seed_run("AAW_TASK_TEST02", ["Napraw błąd A", "Dodaj B", "Dodaj C"])
    seed_run("AAW_TASK_TEST03", ["Uzupełnij README", "Dodaj D"], length=2)
    data = pv.experience_view({})
    assert data["demo"] is False and len(data["runs"]) == 2
    assert set(data["benchmark"]["kinds"]) >= {"BUGFIX", "FEATURE", "DOCS"}
    assert data["horizon"]["enough"] is False and data["structure"]["calls"] > 0
    json.dumps(data)                                            # fully serialisable for the API


def test_experience_view_on_a_clean_install_is_empty_not_broken(home):
    data = pv.experience_view({})
    assert data["benchmark"]["kinds"] == {} and data["runs"] == [] and data["horizon"]["enough"] is False


def test_demo_preview_is_labelled_and_never_touches_real_data(home):
    seed_run("AAW_TASK_TEST04", ["Dodaj A", "Dodaj B"], length=2)
    demo = pv.experience_view({"demo": "web"})
    assert demo["demo"] is True and demo["persona"] == "web" and "UI" in demo["benchmark"]["kinds"]
    real = pv.experience_view({})
    assert real["demo"] is False and "UI" not in real["benchmark"]["kinds"]
    assert pv.experience_view({"demo": "backend"})["benchmark"]["overall"][0]["profile_id"]


def test_forecast_for_a_goal_before_start(home):
    seed_run("AAW_TASK_TEST05", ["Napraw błąd A", "Napraw błąd B", "Napraw błąd C"])
    out = pv.forecast_view({"goal": "Napraw błąd w eksporcie", "directions": []})
    assert out["kind"] == "BUGFIX" and out["note"] is not None or out["per_iteration"] is not None
    assert pv.forecast_view({"goal": "Zbadaj wydajność", "directions": ["raport"]})["kind"] == "ANALYSIS"


def test_a_broken_experience_layer_never_hides_the_run(home, monkeypatch):
    seed_run("AAW_TASK_TEST06", ["Dodaj A", "Dodaj B"], length=2)
    monkeypatch.setattr(pv.ex, "live_view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    view = pv.run_view("AAW_TASK_TEST06")
    assert "boom" in view["live"]["error"] and view["status"] and view["timeline"]


def test_routes_are_registered():
    import inspect
    import product_server
    source = inspect.getsource(product_server.make_handler)
    assert '"/api/experience"' in source and '"/api/experience/forecast"' in source


def test_the_live_block_is_computed_once_per_change_and_not_at_all_for_the_home_list(home, monkeypatch):
    seed_run("AAW_TASK_CACHE01", ["Dodaj A", "Dodaj B"], length=2)
    calls = []
    real = pv._compute_live_and_settlement
    monkeypatch.setattr(pv, "_compute_live_and_settlement", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    first, second = pv.run_view("AAW_TASK_CACHE01"), pv.run_view("AAW_TASK_CACHE01")
    assert len(calls) == 1 and first["live"] == second["live"]
    pv.home_view()
    assert len(calls) == 1                                           # Home cards never build the live block
    seed_run("AAW_TASK_CACHE02", ["Dodaj C", "Dodaj D"], length=2)  # another run's evidence changes history: recompute
    pv.run_view("AAW_TASK_CACHE01")
    assert len(calls) == 2
