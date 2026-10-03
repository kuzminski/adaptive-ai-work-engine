#!/usr/bin/env python3
"""Deterministic "new user" walkthrough of the AAW app (release validation, not a unit test).

Drives the REAL app in a real browser (Playwright/Chromium) exactly the way a
first-time user would: launch → welcome → 7-step wizard → START → watch the
phases → STOP SAFELY → RESUME from Home → Human Gate → Accept. Optionally a
second run exercises STOP NOW during implementation.

Only the AI model is scripted: `product_fake_cli.py` stands in for the
`claude` / `codex` executables (so the run is deterministic and costs
nothing). The app, the background worker, the V0.3 engine, Git worktree,
ledger and run lock are the real ones. The project repository has a bare
remote so "no merge / no push" is measured.

    python packaging/first_user_walkthrough.py                       # from source
    python packaging/first_user_walkthrough.py --exe dist/AAW/AAW    # the portable build
    options: --providers claude,codex  --out EVIDENCE/...  --stop-now  --headed

Writes <out>/walkthrough_report.json and screenshots. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "product_fake_cli.py"

SCENARIO = {
    # implementation takes a few seconds so the user can see the phase and press STOP SAFELY
    "IMPLEMENTER": [
        {"sleep": 4, "write_files": {"src/expenses.py": "EXPENSES = []\n"},
         "output": {"summary": "Added the expense model and JSON storage with tests",
                    "changed_files": ["src/expenses.py"],
                    "checks": [{"name": "unit tests", "status": "PASS", "summary": "3 tests OK"}],
                    "deviations": [], "uncertainties": []}},
        {"sleep": 2, "write_files": {"src/cli.py": "def main():\n    return 0\n"},
         "output": {"summary": "Added terminal commands", "changed_files": ["src/cli.py"],
                    "checks": [{"name": "unit tests", "status": "PASS", "summary": "5 tests OK"}],
                    "deviations": [], "uncertainties": []}}],
    "REVIEWER": [
        {"sleep": 1, "output": {"verdict": "REPAIR_REQUIRED", "summary": "Negative amounts are accepted",
                                "findings": [{"finding_key": "F1", "severity": "HIGH", "file": "src/expenses.py",
                                              "summary": "negative amount accepted", "blocking": True,
                                              "evidence_ref": "RAW_DIFF", "finding_code": None}],
                                "raw_evidence_requests": [], "uncertainties": []}},
        {"sleep": 1, "output": {"verdict": "PASS", "summary": "F1 fixed", "findings": [],
                                "raw_evidence_requests": [], "uncertainties": []}}],
    "REPAIRER": [{"sleep": 1, "write_files": {"src/validate.py": "def ok(a):\n    return a >= 0\n"},
                  "output": {"summary": "Rejects negative amounts", "addressed_findings": ["F1"],
                             "changed_files": ["src/validate.py"],
                             "checks": [{"name": "unit tests", "status": "PASS", "summary": "4 tests OK"}],
                             "uncertainties": []}}],
}
STOP_NOW_SCENARIO = {"IMPLEMENTER": [{"sleep": 30, "write_files": {"src/slow.py": "X = 1\n"},
                                      "output": {"summary": "slow", "changed_files": ["src/slow.py"], "checks": [],
                                                 "deviations": [], "uncertainties": []}}]}


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=u", "-c", "user.email=u@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_sandbox(base: Path, providers: list[str]) -> dict:
    bindir = base / "bin"
    bindir.mkdir(parents=True)
    for harness in providers:
        if os.name == "nt":
            (bindir / f"{harness}.cmd").write_text(f'@"{sys.executable}" "{FAKE}" --as {harness} %*\r\n')
        else:
            wrapper = bindir / harness
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" --as {harness} "$@"\n')
            wrapper.chmod(0o755)
    remote = base / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = base / "my-project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# My project\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    return {"bindir": bindir, "repo": repo, "remote": remote, "main_before": git(repo, "rev-parse", "main"),
            "remote_before": git(repo, "ls-remote", "origin")}


class Report:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.t0 = time.time()
        self.steps: list[dict] = []
        self.checks: list[dict] = []
        self.friction: list[str] = []
        self.phases: list[dict] = []
        self.shots: list[str] = []

    def step(self, name: str, interactions: int, note: str = "") -> None:
        self.steps.append({"step": name, "t_s": round(time.time() - self.t0, 2), "user_interactions": interactions,
                           "note": note})

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)

    def shot(self, page, name: str) -> None:
        path = self.out / f"{len(self.shots) + 1:02d}_{name}.png"
        page.screenshot(path=str(path), full_page=True)
        self.shots.append(path.name)


def wait_text(page, selector: str, predicate, timeout: float = 60.0, interval: float = 0.25) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = page.locator(selector).first.inner_text(timeout=1000)
        except Exception:
            last = ""
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"timeout waiting on {selector}; last text: {last!r}")


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    base = Path(tempfile.mkdtemp(prefix="aaw_walk_"))
    sandbox = make_sandbox(base, [p for p in args.providers.split(",") if p])
    scenario = base / "scenario.json"
    scenario.write_text(json.dumps(SCENARIO))
    env = dict(os.environ)
    system_path = [str(Path(shutil.which("git")).parent)] + (["/usr/bin", "/bin"] if os.name != "nt" else
                                                            os.environ["PATH"].split(os.pathsep))
    env.update(PATH=os.pathsep.join([str(sandbox["bindir"]), *dict.fromkeys(system_path)]),
               AAW_PRODUCT_HOME=str(base / "aaw_home"), AAW_FAKE_SCENARIO=str(scenario),
               AAW_FAKE_CALLS=str(base / "calls.jsonl"))
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "AAW_STATS_ROOT", "AAW_ROUTING_ROOT"):
        env.pop(name, None)
    port = free_port()
    argv = [args.exe] if args.exe else [sys.executable, str(ROOT / "AAW.py")]
    report = Report(out)
    app = subprocess.Popen([*argv, "--no-browser", "--port", str(port)], env=env, cwd=str(base),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}/"
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        else:
            raise AssertionError("app did not start")
        report.step("app launched (AAW.exe → UI served)", 1, "double-click AAW.exe; browser opens automatically")
        with sync_playwright() as pw:
            launch = {"headless": not args.headed}
            if args.chromium:
                launch["executable_path"] = args.chromium
            browser = pw.chromium.launch(**launch)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(url)
            # First launch: the wizard opens by itself with the welcome screen.
            page.wait_for_selector("text=Witaj w AAW", timeout=20000)
            report.check("first launch opens the welcome screen automatically", True)
            report.shot(page, "welcome")
            page.click("#go")
            report.step("welcome read → Zaczynamy", 1)
            # 1. project
            page.wait_for_selector("#repo")
            page.fill("#repo", str(sandbox["repo"]))
            page.click("#check")
            wait_text(page, "#repo-status", lambda t: "Gotowe" in t, 20)
            report.shot(page, "step1_project")
            report.check("project check explains isolation (no worktree knowledge needed)",
                         "izolowanej" in page.inner_text("#repo-status"))
            page.click("#next")
            report.step("1 project selected", 3, "Wybierz… opens a native folder dialog; here the path is typed")
            # 2. providers
            page.wait_for_selector(".pcard")
            report.shot(page, "step2_providers")
            text = page.inner_text("#wiz")
            report.check("provider detection shows FOUND/NOT FOUND, version and login",
                         "FOUND" in text and "9.9.9" in text and "zalogowano" in text)
            page.click("#next")
            report.step("2 AI tools detected", 1)
            # 3. models
            page.wait_for_selector(".group")
            report.shot(page, "step3_models_before_check")
            verify = page.locator("#verify")
            interactions = 1
            if verify.count():
                verify.click()
                interactions += 1
                page.wait_for_function("() => !document.querySelector('#verify') || !document.querySelector('#verify').disabled",
                                       timeout=60000)
                time.sleep(0.5)
            report.shot(page, "step3_models")
            under = page.inner_text("#wiz")
            report.check("actual models are shown under the simple levels", "PLANOWANIE" in under and "/" in under)
            page.click("#next")
            report.step("3 model setup confirmed", interactions)
            # 4–6 goal / first iteration / direction (an example fills all three)
            page.wait_for_selector("#goal")
            page.click("[data-example='0']")
            report.shot(page, "step4_goal")
            page.click("#next")
            page.wait_for_selector("#first")
            page.click("#next")
            page.wait_for_selector("#dirs")
            report.shot(page, "step6_direction")
            page.click("#next")
            report.step("4–6 goal, first iteration, direction (example used)", 4,
                        "a real user types 1–3 sentences here instead of using an example")
            # 7. summary → START
            page.wait_for_selector("#start:not([disabled])", timeout=30000)
            report.shot(page, "step7_summary")
            page.click("#start")
            t_start = time.time() - report.t0
            report.step("START pressed", 1)
            page.wait_for_selector(".banner", timeout=30000)
            # Watch the active phase change.
            seen: list[str] = []
            stopped = False
            deadline = time.time() + 120
            while time.time() < deadline:
                banner = page.locator(".banner").first.inner_text(timeout=2000)
                if not seen or seen[-1] != banner:
                    seen.append(banner)
                    report.phases.append({"t_s": round(time.time() - report.t0, 2), "banner": banner.replace("\n", " | ")})
                if "Implementacja" in banner and not stopped:
                    report.shot(page, "process_implementation")
                    page.click("#btn-stop")
                    stopped = True
                    report.step("STOP SAFELY pressed during implementation", 1)
                if stopped and "Wstrzymane" in banner:
                    break
                time.sleep(0.3)
            report.check("active phase visible and changing (banner)", len(seen) >= 3, " → ".join(seen))
            report.check("STOP SAFELY during implementation pauses after the step", stopped and "Wstrzymane" in seen[-1])
            report.shot(page, "paused")
            pause_text = page.inner_text("#view")
            report.check("paused run offers RESUME with an explanation", "RESUME" in pause_text)
            # Home shows the paused run with RESUME (as after an app restart).
            page.click("a[data-nav='home']")
            page.wait_for_selector(".home-sec.paused .card")
            report.shot(page, "home_paused")
            report.check("Home lists the paused run under WSTRZYMANE with RESUME",
                         page.locator(".home-sec.paused [data-resume]").count() == 1)
            page.click(".home-sec.paused [data-resume]")
            report.step("RESUME pressed on Home", 2)
            seen_after: list[str] = []
            deadline = time.time() + 180
            while time.time() < deadline:
                banner = page.locator(".banner").first.inner_text(timeout=2000) if page.locator(".banner").count() else ""
                if banner and (not seen_after or seen_after[-1] != banner):
                    seen_after.append(banner)
                    report.phases.append({"t_s": round(time.time() - report.t0, 2), "banner": banner.replace("\n", " | ")})
                    if "Naprawa" in banner:
                        report.shot(page, "process_repair")
                if "Czeka na Twoją decyzję" in banner:
                    break
                time.sleep(0.3)
            report.check("repair phase was visible", any("Naprawa" in b for b in seen_after), " → ".join(seen_after))
            report.check("run reached the Human Gate", any("Czeka na Twoją decyzję" in b for b in seen_after))
            page.wait_for_selector(".panel.gate")
            gate = page.inner_text(".panel.gate")
            for section in ("Dlaczego AAW się zatrzymał", "Co zrobiono", "Co zostało", "Ostrzeżenia", "Bieżący kandydat"):
                report.check(f"Human Gate shows: {section}", section.lower() in gate.lower())
            report.check("Human Gate explains roadmap exhaustion", "Roadmapa wyczerpana" in gate)
            report.shot(page, "human_gate")
            # Briefs + raw evidence
            page.locator("details.iter").first.locator("summary").click()
            label = page.locator("details.iter .label").first.inner_text()
            report.check("iteration 1 timeline shows REPAIR → PASS", label == "REPAIR → PASS", label)
            brief = page.locator("details.iter").first.inner_text().lower()
            for field in ("Zadanie etapu", "Co zrobiono", "Testy / checki", "Problemy / niepewności", "Przekazano dalej"):
                report.check(f"brief field: {field}", field.lower() in brief)
            page.locator("[data-evidence]").first.click()
            page.wait_for_selector("#modal:not([hidden]) pre.raw")
            report.check("View raw evidence opens the engine artifacts", "execution_id" in page.inner_text("pre.raw"))
            report.shot(page, "raw_evidence")
            page.click("#modal-close")
            page.click("#g-accept")
            wait_text(page, ".panel.gate", lambda t: "READY_FOR_EXTERNAL_INTEGRATION" in t and "ACCEPTED" in t, 30)
            report.shot(page, "accepted")
            report.step("Accept pressed (confirmed)", 2)
            report.check("Accept = READY_FOR_EXTERNAL_INTEGRATION", True)
            if args.stop_now:
                stop_now_flow(page, report, sandbox, scenario)
            browser.close()
        repo = sandbox["repo"]
        report.check("canonical main unchanged (no merge)", git(repo, "rev-parse", "main") == sandbox["main_before"])
        report.check("remote unchanged (no push)", git(repo, "ls-remote", "origin") == sandbox["remote_before"])
        report.check("canonical checkout clean (not modified)", git(repo, "status", "--porcelain") == "")
        report_data = {
            "schema": "AAW_FIRST_USER_WALKTHROUGH_V1", "app": " ".join(argv), "providers": args.providers,
            "platform": sys.platform, "launch_to_start_s": round(t_start, 2),
            "launch_to_start_user_interactions": sum(s["user_interactions"] for s in report.steps
                                                    if report.steps.index(s) <= next(i for i, x in enumerate(report.steps) if x["step"] == "START pressed")),
            "steps": report.steps, "phases_seen": report.phases, "checks": report.checks,
            "screenshots": report.shots, "passed": all(c["ok"] for c in report.checks),
            "note": "Automated user: times exclude human reading/typing. The AI model is scripted (fake CLI); "
                    "app, worker, engine, Git, ledger and lock are real.",
        }
        (out / "walkthrough_report.json").write_text(json.dumps(report_data, indent=2, ensure_ascii=False) + "\n",
                                                     encoding="utf-8")
        print(json.dumps({k: report_data[k] for k in ("launch_to_start_s", "launch_to_start_user_interactions",
                                                      "passed")}, indent=2))
        return 0 if report_data["passed"] else 1
    finally:
        try:
            token = json.loads((base / "aaw_home" / "ui.json").read_text())["token"]
            request = urllib.request.Request(url + "api/quit", data=b"{}", headers={
                "X-AAW-Token": token, "Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=5).read()
        except Exception:
            pass
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


def stop_now_flow(page, report: Report, sandbox: dict, scenario: Path) -> None:
    """Second task: STOP NOW during implementation → RESUME never replays it blindly → Human Gate explains."""
    scenario.write_text(json.dumps(STOP_NOW_SCENARIO))
    calls_file = scenario.with_name("calls.jsonl")
    before = sum(1 for line in calls_file.read_text().splitlines() if json.loads(line).get("role") == "IMPLEMENTER")
    page.click("a[data-nav='new']")
    page.wait_for_selector("#repo")
    page.fill("#repo", str(sandbox["repo"]))
    page.click("#check")
    wait_text(page, "#repo-status", lambda t: "Gotowe" in t, 20)
    page.click("#next")
    page.wait_for_selector(".pcard")
    page.click("#next")
    page.wait_for_selector(".group")
    page.click("#next")
    page.fill("#goal", "Slow task used to try STOP NOW")
    page.click("#next")
    page.click("#next")
    page.click("#next")
    page.wait_for_selector("#start:not([disabled])", timeout=30000)
    page.click("#start")
    wait_text(page, ".banner", lambda t: "Implementacja" in t, 90)
    page.click("#btn-force")
    wait_text(page, ".banner", lambda t: "Wstrzymane" in t or "Przerwane" in t, 60)
    report.shot(page, "stop_now_paused")
    report.check("STOP NOW stops a running implementation", True)
    page.click("#btn-resume")
    gate = wait_text(page, ".panel.gate", lambda t: "dlaczego" in t.lower(), 90)
    report.shot(page, "stop_now_gate")
    report.check("interrupted implementation is NOT replayed: Human Gate explains the uncertain effect",
                 "nie powtórzył" in gate or "NIEPEWNE" in gate or "niepewn" in gate.lower(), gate[:300])
    after = sum(1 for line in calls_file.read_text().splitlines() if json.loads(line).get("role") == "IMPLEMENTER")
    report.check("implementation call was not re-run after STOP NOW", after - before == 1,
                 f"implementer calls in this task: {after - before}")
    report.step("STOP NOW → RESUME → Human Gate", 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--exe", help="path to the built AAW executable (default: run AAW.py from source)")
    parser.add_argument("--providers", default="claude,codex")
    parser.add_argument("--out", default=str(ROOT / "build" / "walkthrough"))
    parser.add_argument("--stop-now", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--chromium", default=os.environ.get("AAW_WALK_CHROMIUM"),
                        help="Chromium executable (when the Playwright-managed browser is not installed)")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox folder")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
