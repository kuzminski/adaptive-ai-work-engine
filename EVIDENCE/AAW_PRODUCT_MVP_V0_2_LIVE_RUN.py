#!/usr/bin/env python3
"""AAW Product MVP V0.2 — live smoke with the REAL Claude CLI (Linux; costs real usage).

Drives the real app (source) in a real browser through the first-run wizard,
including "Sprawdź modele" (real model probes), START, the autonomous run and
the Human Gate (Accept). Records the bindings that actually ran, the ledger
lifecycle, provider-reported cost and the Git no-merge/no-push checks.

    python EVIDENCE/AAW_PRODUCT_MVP_V0_2_LIVE_RUN.py --chromium /path/to/chrome

Writes EVIDENCE/AAW_PRODUCT_MVP_V0_2_LIVE_E2E.json and screenshots into
EVIDENCE/AAW_PRODUCT_MVP_V0_2_SCREENSHOTS/live_*.png.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
from first_user_walkthrough import free_port, git, wait_text  # noqa: E402

OUT = ROOT / "EVIDENCE" / "AAW_PRODUCT_MVP_V0_2_LIVE_E2E.json"
SHOTS = ROOT / "EVIDENCE" / "AAW_PRODUCT_MVP_V0_2_SCREENSHOTS"
GOAL = "A tiny Python text utility module with tests"
FIRST = "Add slugify(text) in textutil.py that lowercases, trims and joins words with '-', with unittest tests"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", default=os.environ.get("AAW_WALK_CHROMIUM"))
    parser.add_argument("--timeout-min", type=int, default=45)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    claude = shutil.which("claude")
    assert claude, "the real Claude CLI must be on PATH"
    base = Path(tempfile.mkdtemp(prefix="aaw_live_"))
    remote = base / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = base / "textutil-project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# textutil\n\nSmall text helpers. Run tests: python -m unittest\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    main_before, remote_before = git(repo, "rev-parse", "main"), git(repo, "ls-remote", "origin")
    env = dict(os.environ, AAW_PRODUCT_HOME=str(base / "aaw_home"))
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "AAW_STATS_ROOT", "AAW_ROUTING_ROOT"):
        env.pop(name, None)
    port = free_port()
    app = subprocess.Popen([sys.executable, str(ROOT / "AAW.py"), "--no-browser", "--port", str(port)], env=env,
                           cwd=str(base), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/"
    SHOTS.mkdir(parents=True, exist_ok=True)
    timeline: list[dict] = []
    t0 = time.time()
    record: dict = {"schema": "AAW_PRODUCT_MVP_V0_2_LIVE_E2E", "platform": sys.platform,
                    "claude_cli": subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip()}
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(url, timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        with sync_playwright() as pw:
            launch = {"headless": True}
            if args.chromium:
                launch["executable_path"] = args.chromium
            browser = pw.chromium.launch(**launch)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("dialog", lambda d: d.accept())
            page.goto(url)
            page.wait_for_selector("text=Witaj w AAW", timeout=30000)
            page.click("#go")
            page.fill("#repo", str(repo))
            page.click("#check")
            wait_text(page, "#repo-status", lambda t: "Gotowe" in t, 30)
            page.click("#next")
            page.wait_for_selector(".pcard", timeout=60000)
            page.screenshot(path=str(SHOTS / "live_01_providers.png"), full_page=True)
            record["providers_step"] = page.inner_text("#wiz")
            page.click("#next")
            page.wait_for_selector(".group", timeout=60000)
            page.screenshot(path=str(SHOTS / "live_02_models_before_check.png"), full_page=True)
            record["models_before_check"] = page.inner_text("#wiz")
            t_probe = time.time()
            page.click("#verify")
            page.wait_for_function("() => !document.querySelector('#verify') || !document.querySelector('#verify').disabled",
                                   timeout=300000)
            record["probe_seconds"] = round(time.time() - t_probe, 1)
            time.sleep(1)
            page.screenshot(path=str(SHOTS / "live_03_models_after_check.png"), full_page=True)
            record["models_after_check"] = page.inner_text("#wiz")
            page.click("#next")
            page.fill("#goal", GOAL)
            page.click("#next")
            page.fill("#first", FIRST)
            page.click("#next")
            page.click("#next")
            page.wait_for_selector("#start:not([disabled])", timeout=60000)
            page.screenshot(path=str(SHOTS / "live_04_summary.png"), full_page=True)
            record["summary"] = page.inner_text("#wiz")
            page.click("#start")
            record["launch_to_start_s"] = round(time.time() - t0, 1)
            page.wait_for_selector(".banner", timeout=60000)
            deadline = time.time() + args.timeout_min * 60
            shot = 0
            last = ""
            while time.time() < deadline:
                banner = page.locator(".banner").first.inner_text(timeout=5000)
                if banner != last:
                    last = banner
                    timeline.append({"t_s": round(time.time() - t0, 1), "banner": banner.replace("\n", " | ")})
                    if shot < 6:
                        shot += 1
                        page.screenshot(path=str(SHOTS / f"live_1{shot}_process.png"), full_page=True)
                if "Czeka na Twoją decyzję" in banner or "Wymaga uwagi" in banner:
                    break
                time.sleep(2)
            page.wait_for_selector(".panel.gate", timeout=60000)
            page.screenshot(path=str(SHOTS / "live_20_gate.png"), full_page=True)
            record["gate_text"] = page.inner_text(".panel.gate")
            if page.locator("#g-accept").count():
                page.click("#g-accept")
                wait_text(page, ".panel.gate", lambda t: "READY_FOR_EXTERNAL_INTEGRATION" in t, 60)
                page.screenshot(path=str(SHOTS / "live_21_accepted.png"), full_page=True)
                record["accepted"] = True
            browser.close()
        home = Path(env["AAW_PRODUCT_HOME"])
        run_dir = next((home / "runs").iterdir())
        state = json.loads((run_dir / "AUTONOMY" / "autonomy_state.json").read_text())
        task = json.loads((run_dir / "PRODUCT" / "task.json").read_text())
        probes = json.loads((home / "model_probes.json").read_text())
        ledger_rows = [json.loads(line) for line in (run_dir / "LEDGER" / "execution_events.jsonl").read_text().splitlines()]
        closed = {r["execution_id"]: r.get("payload", {}).get("close_reason") for r in ledger_rows
                  if r.get("event_type") in ("EXECUTION_CLOSED",)}
        def costs(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "total_cost_usd" and isinstance(item, (int, float)):
                        yield float(item)
                    else:
                        yield from costs(item)
            elif isinstance(value, list):
                for item in value:
                    yield from costs(item)
        cost = sum(sum(costs(json.loads(f.read_text()))) for f in (run_dir / "AUTONOMY" / "RESULTS").glob("*.json"))
        probe_cost = 0.0  # probes print their own JSON; not stored by AAW
        record.update({
            "phase_timeline": timeline, "run_id": run_dir.name,
            "engine_status": state["status"], "hold": state.get("hold"), "escalation": state.get("escalation"),
            "promotion": state.get("promotion"), "main_merge_allowed": state.get("main_merge_allowed"),
            "probes": probes.get("probes"),
            "resolved_slots": {k: {f: v.get(f) for f in ("profile_id", "status", "exact_mapping_of",
                                                         "runtime_model_id", "effort")}
                               for k, v in task["resolution"]["slots"].items()},
            "executions": [{f: e.get(f) for f in ("execution_id", "role", "executor", "profile", "model", "effort",
                                                  "phase", "iteration_id")} for e in state.get("executions", [])],
            "ledger_closed": closed, "ledger_event_count": len(ledger_rows),
            "provider_reported_cost_usd_from_results": round(cost, 4),
            "git": {"main_unchanged": git(repo, "rev-parse", "main") == main_before,
                    "remote_unchanged": git(repo, "ls-remote", "origin") == remote_before,
                    "canonical_clean": git(repo, "status", "--porcelain") == ""},
            "total_wall_s": round(time.time() - t0, 1),
        })
        OUT.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({k: record[k] for k in ("engine_status", "git", "provider_reported_cost_usd_from_results",
                                                 "total_wall_s")}, indent=2))
        return 0
    finally:
        try:
            token = json.loads((base / "aaw_home" / "ui.json").read_text())["token"]
            urllib.request.urlopen(urllib.request.Request(url + "api/quit", data=b"{}", headers={
                "X-AAW-Token": token, "Content-Type": "application/json"}), timeout=5).read()
        except Exception:
            pass
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
        print("sandbox:", base)


if __name__ == "__main__":
    raise SystemExit(main())
