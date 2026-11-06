"""Roadmap 2.5: project templates are plain, bounded, editable data."""
import product_presets as pp
import product_runs as prun

KINDS = {"cli", "web_api", "library", "ui_app"}


def test_templates_cover_the_four_kinds_with_unique_ids():
    ts = pp.list_templates()
    assert {t["kind"] for t in ts} == KINDS
    assert len({t["id"] for t in ts}) == len(ts)


def test_every_template_has_criteria_evidence_and_fits_limits():
    for t in pp.list_templates():
        for key in ("title", "goal", "first_iteration", "directions"):
            assert t[key].strip(), (t["id"], key)
        adv = t["advanced"]
        assert adv["acceptance_criteria"].strip() and adv["required_evidence"].strip(), t["id"]
        assert len(t["goal"]) <= prun.MAX_FIELD_CHARS and len(t["first_iteration"]) <= prun.MAX_FIELD_CHARS
        assert len(t["directions"]) <= prun.MAX_ROADMAP_CHARS
        assert len([l for l in t["directions"].splitlines() if l.strip()]) <= prun.MAX_DIRECTIONS
        for r in adv["risks"]:
            assert r["severity"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL") and r["description"]


def test_get_template_returns_a_copy():
    t = pp.get_template("tpl_cli")
    t["goal"] = "x"
    assert pp.get_template("tpl_cli")["goal"] != "x"
    assert pp.get_template("nope") is None
