"""Long-form project fields, example presets and unbounded-by-count autonomy.

Covers the cloud/local autonomy pass: nothing a user types into Goal / First iteration / Direction is
truncated anywhere between the form and the frozen mandate; presets are plain examples; the engine has no
`iteration_count >= 2`-style rule (the loop PLAN → … → FINAL_REVIEW → ROADMAP_CHECK → PLAN continues while a
standing roadmap item offers justified work); and the execution fuses (iteration cap, STOP, planner's reasoned
end, escalation) still stop it. Same real stack as the product tests (worker process + engine + fake CLI).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import autonomy_contract as ac  # noqa: E402
import product_presets  # noqa: E402
import product_runs as prun  # noqa: E402
import product_view as pv  # noqa: E402
from test_product_mvp import assert_no_merge_push, env, form, settled, start, wait_for  # noqa: E402,F401

LONG = ("# Specyfikacja\n" + "Zażółć gęślą jaźń — żółć, ąę, 日本語, emoji 🚀, `code`, {json: \"x\"}, <b>&amp;</b>\n" * 1600)


def state_of(run_id):
    return json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text(encoding="utf-8"))


# ── field limits ─────────────────────────────────────────────────────────────

def test_normalize_form_keeps_very_long_text_untouched():
    goal = LONG
    first = "x" * 150_000
    roadmap = "\n".join(f"- punkt {i}: " + "dłuuugi opis " * 200 for i in range(150))
    out = prun.normalize_form({"goal": goal, "first_iteration": first, "directions": roadmap})
    assert out["goal"] == goal.strip() and len(out["goal"]) > 100_000
    assert out["first_iteration"] == first
    assert len(out["directions"]) == 150                      # was capped at 12 points of 400 chars
    assert out["directions"][149].startswith("punkt 149: ") and len(out["directions"][0]) > 2500
    assert out["directions_text"] == roadmap.strip()          # original text kept verbatim


def test_mandate_carries_full_text_and_validates():
    out = prun.normalize_form({"goal": LONG, "first_iteration": LONG + "!", "directions": ["- a", "- b"]})
    mandate = ac.validate_mandate(prun.build_mandate(out, "M1", {}))
    contract, roadmap = mandate["iteration_contract"], mandate["roadmap_mandate"]
    assert contract["goal"] == (LONG + "!").strip()
    assert roadmap["objective"] == LONG.strip()
    assert roadmap["items"][0]["title"] == (LONG + "!").strip()   # no [:400]
    assert "direction_text" in roadmap and ac.mandate_hash_ok(mandate)


def test_safety_ceiling_is_a_clear_error_never_a_silent_cut():
    with pytest.raises(prun.ProductError, match="bezpiecznik"):
        prun.normalize_form({"goal": "x" * (prun.MAX_FIELD_CHARS + 1)})
    with pytest.raises(prun.ProductError, match="bezpiecznik"):
        prun.normalize_form({"goal": "valid goal", "directions": "- " + "y" * (prun.MAX_DIRECTION_CHARS + 1)})
    with pytest.raises(prun.ProductError, match="punkt"):
        prun.normalize_form({"goal": "valid goal", "directions": [f"p{i}" for i in range(prun.MAX_DIRECTIONS + 1)]})


def test_split_directions_keeps_legacy_lists_and_markdown_structure():
    assert prun.split_directions(["- one", "2) two", "three"]) == ["one", "two", "three"]
    assert prun.split_directions("a\n\nb\n") == ["a", "b"]
    md = "# Faza 1\n- API\n  - endpoint /x\n  - endpoint /y\n\n  Uwaga: wersjonowanie\n- UI\n\n## Faza 2\n1. Testy\n"
    points = prun.split_directions(md)
    assert [p.splitlines()[0] for p in points] == ["API", "UI", "Testy"]
    assert "endpoint /y" in points[0] and "Uwaga: wersjonowanie" in points[0]
    assert prun.split_directions("") == [] and prun.split_directions(None) == []


def test_legacy_form_shape_still_works():
    out = prun.normalize_form({"goal": "Build a tiny greeting module", "first_iteration": "Add greet(name)",
                               "directions": ["multi-language greetings"]})
    assert out["directions"] == ["multi-language greetings"]
    mandate = ac.validate_mandate(prun.build_mandate(out, "M2", {}))
    ids = [i["item_id"] for i in mandate["roadmap_mandate"]["items"]]
    assert ids == ["STEP_1", "STEP_2", prun.CONTINUATION_ITEM_ID]


def test_long_text_survives_start_save_and_reopen(env):
    env.install("claude")
    run_id = start(env, goal=LONG, first_iteration=LONG[:60_000],
                   directions="- pierwszy\n  szczegół pierwszego\n- drugi")
    task = prun.load_task(run_id)
    assert task["form"]["goal"] == LONG.strip()
    assert task["form"]["first_iteration"] == LONG[:60_000].strip()
    assert task["form"]["directions"][0] == "pierwszy\n  szczegół pierwszego"
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE
    assert view["goal"] == LONG.strip()                                   # reopened view: not cut
    assert view["direction"]["goal"] == LONG.strip() and view["direction"]["frozen"]
    assert "szczegół pierwszego" in view["direction"]["directions_text"]
    assert state_of(run_id)["mandate"]["roadmap_mandate"]["objective"] == LONG.strip()
    assert_no_merge_push(env)


# ── presets ──────────────────────────────────────────────────────────────────

def test_presets_are_plain_editable_examples_that_form_valid_tasks():
    presets = product_presets.list_presets()
    assert {"autonomous_mvp"} <= {p["id"] for p in presets} and len({p["id"] for p in presets}) == len(presets)
    for p in presets:
        out = prun.normalize_form({"goal": p["goal"], "first_iteration": p["first_iteration"],
                                   "directions": p["directions"]})
        ac.validate_mandate(prun.build_mandate(out, "P", {}))
    mvp = product_presets.get_preset("autonomous_mvp")
    assert "PLAN → IMPLEMENT → VERIFY → REVIEW → REPAIR → NEXT PLAN" in mvp["goal"]
    assert len(prun.split_directions(mvp["directions"])) >= 4
    assert product_presets.get_preset("nope") is None
    mvp["goal"] = "changed"                                                 # copies: editing never leaks back
    assert product_presets.get_preset("autonomous_mvp")["goal"] != "changed"


# ── contract: continuation + fuses ───────────────────────────────────────────

def test_hard_caps_are_fuses_not_two_and_old_values_stay_valid():
    assert ac.HARD_MAX_ITERATIONS >= 100 and ac.DEFAULT_MAX_ITERATIONS > 2
    base = ac.validate_mandate(prun.build_mandate(
        prun.normalize_form({"goal": "Build something", "advanced": {"max_iterations": ac.HARD_MAX_ITERATIONS}}), "M3", {}))
    assert base["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] == ac.HARD_MAX_ITERATIONS
    raw = prun.build_mandate(prun.normalize_form({"goal": "Build something"}), "M4", {})
    raw["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] = 50      # a V0.3-era stored value
    ac.validate_mandate(raw)
    raw["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] = ac.HARD_MAX_ITERATIONS + 1
    with pytest.raises(ac.AutonomyError):
        ac.validate_mandate(raw)
    bad = prun.build_mandate(prun.normalize_form({"goal": "Build something"}), "M5", {})
    bad["roadmap_mandate"]["items"][-1]["recurring"] = "yes"
    with pytest.raises(ac.AutonomyError, match="recurring"):
        ac.validate_mandate(bad)


def test_old_mandates_without_the_new_fields_behave_as_before():
    legacy = prun.build_mandate(prun.normalize_form({"goal": "Build something", "directions": ["more"],
                                                     "advanced": {"continue_autonomously": False}}), "M6", {})
    assert [i["item_id"] for i in legacy["roadmap_mandate"]["items"]] == ["STEP_1", "STEP_2"]
    assert legacy["roadmap_mandate"]["autonomy_bounds"]["max_iterations"] == 4      # len(items)+2, as in V0.3
    roadmap = ac.initial_roadmap(ac.validate_mandate(legacy))
    assert not any(r.get("recurring") for r in roadmap.values())
    assert ac.remaining_items(roadmap) == ["STEP_1", "STEP_2"]


# ── end to end: the loop does not stop at 2 ──────────────────────────────────

def test_run_continues_past_two_iterations_until_the_planner_ends_it_with_a_reason(env):
    env.install("claude")
    env.monkeypatch.setenv("AAW_FAKE_CONTINUATION_PASSES", "4")
    run_id = start(env, directions=[])
    view = wait_for(run_id, settled, timeout=240)
    assert view["status"] == pv.S_GATE
    state = state_of(run_id)
    accepted = [i for i in state["iterations"] if i["status"] == "ACCEPTED"]
    assert len(accepted) == 5                                   # STEP_1 + 4 continuation passes — well beyond 2
    assert all(i["outcome"] == "PASS" for i in accepted)
    standing = state["roadmap"][prun.CONTINUATION_ITEM_ID]
    assert standing["status"] == ac.R_SKIPPED and "no sensible further work" in standing["reason"]
    assert len(standing["iterations"]) == 4
    assert state["hold"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED and state["hold"]["promotable"]
    # FINAL_REVIEW → ROADMAP_CHECK → next PLAN, with no human in between
    events = [json.loads(l) for l in (prun.autonomy_dir(run_id) / "autonomy_events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["event_type"] == "ROADMAP_DECISION"]
    assert [d["payload"]["next_action_available"] for d in decisions][:5] == [True, True, True, True, True]
    assert sum(1 for e in events if e["event_type"] == "AWAITING_HUMAN") == 1
    # the operator sees why, what passed and the planner's own end reason
    assert view["gate"]["planner_end_reason"] and view["gate"]["remaining"] == [] and not view["gate"]["could_continue"]
    assert len(view["gate"]["done"]) == 5
    assert view["process"]["iterations_done"] == 5 and view["process"]["max_iterations"] == ac.DEFAULT_MAX_ITERATIONS
    # the planner's working notes are stored apart from the frozen user direction
    assert state["working_roadmap"]["next_step"] and len(state["working_roadmap_history"]) == 5
    assert view["working_roadmap"]["index"] == 5
    assert view["direction"]["directions_text"] == "" and ac.mandate_hash_ok(state["mandate"])
    assert_no_merge_push(env)


def test_iteration_cap_is_a_fuse_that_hands_over_with_work_left(env):
    env.install("claude")
    env.monkeypatch.setenv("AAW_FAKE_CONTINUATION_PASSES", "50")
    run_id = start(env, directions=[], advanced={"max_iterations": 3})
    view = wait_for(run_id, settled, timeout=240)
    assert view["status"] == pv.S_GATE
    state = state_of(run_id)
    assert len([i for i in state["iterations"] if i["status"] == "ACCEPTED"]) == 3
    assert state["hold"]["reason"] == ac.HOLD_ITERATION_CAP and not state["hold"]["roadmap_exhausted"]
    assert view["gate"]["could_continue"] and view["gate"]["actions"]["accept_needs_early_end"]
    assert "bezpiecznik" in view["gate"]["why"]


def test_manual_stop_pauses_an_autonomous_run_and_resume_goes_on(env):
    env.install("claude")
    env.monkeypatch.setenv("AAW_FAKE_CONTINUATION_PASSES", "3")
    run_id = start(env, directions=[])
    wait_for(run_id, lambda v: (v.get("process") or {}).get("iterations_done", 0) >= 1 or v["status"] == pv.S_GATE,
             timeout=120, interval=0.05)
    prun.request_stop(run_id)
    view = wait_for(run_id, settled, timeout=120)
    assert view["status"] in (pv.S_PAUSED, pv.S_GATE)
    if view["status"] == pv.S_PAUSED:
        assert state_of(run_id)["status"] == ac.RUNNING          # paused, NOT awaiting a human
        assert view["controls"]["resume"]
        prun.resume_task(run_id)
        view = wait_for(run_id, settled, timeout=240)
    assert view["status"] == pv.S_GATE
    assert len([i for i in state_of(run_id)["iterations"] if i["status"] == "ACCEPTED"]) == 4
    assert_no_merge_push(env)


def test_listed_points_only_mode_still_ends_when_they_are_done(env):
    env.install("claude")
    run_id = start(env, advanced={"continue_autonomously": False})
    view = wait_for(run_id, settled, timeout=120)
    assert view["status"] == pv.S_GATE
    state = state_of(run_id)
    assert prun.CONTINUATION_ITEM_ID not in state["roadmap"]
    assert len(state["iterations"]) == 2 and state["hold"]["reason"] == ac.HOLD_ROADMAP_EXHAUSTED


def test_iteration_finish_alone_never_asks_for_a_human(env):
    """AWAITING_HUMAN only on real reasons: after an accepted iteration with work left the run keeps RUNNING."""
    env.install("claude")
    env.monkeypatch.setenv("AAW_FAKE_CONTINUATION_PASSES", "2")
    run_id = start(env, directions=[])
    seen = set()

    def watch(v):
        st = state_of(run_id) if (prun.autonomy_dir(run_id) / "autonomy_state.json").exists() else {}
        if len([i for i in st.get("iterations", []) if i["status"] == "ACCEPTED"]) in (1, 2):
            seen.add(st.get("status"))
        return settled(v)
    wait_for(run_id, watch, timeout=240, interval=0.02)
    assert ac.AWAITING_HUMAN not in seen
