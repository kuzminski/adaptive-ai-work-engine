"""Roadmap 2.2: edited roadmap continuation = new run + explicit diff."""
import autonomy_contract as ac
import product_runs as prun
import roadmap_edit as re_


def test_diff_added_removed_reordered_and_marker_change():
    old = ["Napraw A", "Dodaj B", "[człowiek] Podłącz C", "Dodaj D"]
    new = ["dodaj b", "Napraw A", "Podłącz C", "Nowy E"]
    d = re_.diff_roadmaps(old, new)
    assert d["added"] == ["Nowy E"] and d["removed"] == ["Dodaj D"]
    assert d["reordered"] and d["human_changed"] == ["Podłącz C"] and d["changed"]
    assert "dodano 1" in d["summary"] and "usunięto 1" in d["summary"] and "kolejność" in d["summary"]


def test_diff_identical_is_unchanged_even_with_spacing_and_case():
    d = re_.diff_roadmaps(["A  b", "[human] C"], ["a b", "[człowiek] c"])
    assert not d["changed"] and d["summary"].startswith("bez zmian")


def test_normalize_form_keeps_a_cleaned_roadmap_parent():
    f = prun.normalize_form({"goal": "Zbuduj coś ciekawego",
                             "roadmap_parent": {"run_id": " R1 ", "previous_directions": ["a", "", "b"]}})
    assert f["roadmap_parent"] == {"run_id": "R1", "previous_directions": ["a", "b"]}
    assert prun.normalize_form({"goal": "Zbuduj coś ciekawego"})["roadmap_parent"] is None
    assert prun.normalize_form({"goal": "Zbuduj coś ciekawego", "roadmap_parent": {"run_id": ""}})["roadmap_parent"] is None


def test_roadmap_continuation_carries_only_unfinished_points_and_the_previous_list(monkeypatch):
    import autonomy_controller as ctl
    form = prun.normalize_form({"goal": "Zbuduj coś ciekawego", "first_iteration": "start",
                                "directions": "- Zrobione\n- Do zrobienia\n- [człowiek] Płatności"})
    mandate = prun.build_mandate(form, "M1", {})
    state = {"status": "REJECTED", "mandate": ac.validate_mandate(mandate),
             "roadmap": {"STEP_2": {"status": ac.R_DONE}, "STEP_3": {"status": ac.R_PENDING},
                         "STEP_4": {"status": ac.R_HUMAN_REQUIRED}}}
    monkeypatch.setattr(prun, "load_task", lambda run_id: {"form": form, "workspace": {"repo": "r"}})
    monkeypatch.setattr(ctl, "load_state", lambda run_id, root: state)
    monkeypatch.setattr(prun, "runs_root", lambda: None)
    pf = prun.prepare_continuation("RUN", mode="roadmap")["prefill"]
    assert pf["directions"] == ["Do zrobienia", "[człowiek] Płatności"]
    assert pf["roadmap_parent"] == {"run_id": "RUN", "previous_directions": ["Zrobione", "Do zrobienia", "[człowiek] Płatności"]}
    assert pf["goal"] == form["goal"]
