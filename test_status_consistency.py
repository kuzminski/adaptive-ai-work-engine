"""Roadmap 0.4: one version everywhere, a status page generated from code, no contradictory status claims."""
import json
import re
from pathlib import Path

import product_server
import product_version
import status_page

ROOT = Path(__file__).resolve().parent
REPORTS_WITH_APPROVED = ["AAW_P24_P3_FINAL_REVIEW", "AAW_P25_BENCHMARK_AUDIT", "AAW_P25_EMPIRICAL_BENCHMARK_REPORT",
                         "AAW_P3_ADAPTIVE_POLICY_REPORT", "AAW_P3_SHADOW_MODE_REPORT"]


def test_one_version_everywhere():
    v = product_version.RELEASE
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))["version"]
    readme = re.search(r"## Product app \(MVP V0\.2 .{1,3} release `([^`]+)`\)", (ROOT / "README.md").read_text(encoding="utf-8")).group(1)
    quick = re.search(r"^Release `([^`]+)`", (ROOT / "QUICK_START.md").read_text(encoding="utf-8"), re.M).group(1)
    assert {pyproject, package, lock, readme, quick} == {v}, (v, pyproject, package, lock, readme, quick)
    assert product_server.APP_VERSION == f"AAW {v}"


def test_generated_status_page_is_current_and_reports_the_lock():
    generated = status_page.render()
    committed = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    strip = lambda text: "\n".join(l for l in text.splitlines() if not l.startswith("- Tests collected"))
    assert strip(generated) == strip(committed), "docs/STATUS.md is stale: run `python status_page.py`"
    assert "locked: yes" in generated


def test_reports_that_say_approved_carry_the_locked_mode_correction():
    for name in REPORTS_WITH_APPROVED:
        text = (ROOT / "docs" / "reports" / f"{name}.md").read_text(encoding="utf-8")
        assert "STATUS CORRECTION" in text and "LOCKED" in text and "SIMULATED" in text, name


def test_ui_package_holds_only_the_bundle_index_references():
    """Roadmap 0.5: no stale bundles shipped; the build config empties the output dir."""
    ui = ROOT / "PRODUCT_UI"
    index = (ui / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'/static/(assets/[^"\']+)', index))
    present = {f"assets/{p.name}" for p in (ui / "assets").iterdir()}
    assert referenced and present == referenced, (present, referenced)
    assert "emptyOutDir: true" in (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
