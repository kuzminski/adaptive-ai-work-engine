"""AAW Product MVP V0.1 — product-layer validation.

Runs go through the real stack: product_runs → background worker process
(`AAW.py --run-worker`) → V0.3 AutonomyController → real DirectRoleExecutor →
`product_fake_cli.py` standing in for the `claude` / `codex` binaries (only the
model is scripted). Every Git fixture has a bare remote so "no merge / no
push" is measured, not assumed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import autonomy_contract as ac  # noqa: E402
import product_home  # noqa: E402
import product_providers as pp  # noqa: E402
import product_recommendations as pr  # noqa: E402
import product_runs as prun  # noqa: E402
import product_view as pv  # noqa: E402

CODEX_RUNNABLE = {"GPT6_LUNA_HIGH", "GPT6_LUNA_VERY_HIGH", "GPT6_LUNA_MAX", "SOL_6_1_LIGHT", "SOL_5_6_LIGHT",
                  "ASTRA_HIGH", "ASTRA_XHIGH", "ASTRA_MAX", "SOL_HIGH", "SOL_MEDIUM", "TERRA_HIGH", "LUNA_HIGH",
                  "LUNA_MAX"}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated AAW home, fake CLI bin dir, a project repo with a bare remote."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("AAW_PRODUCT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AAW_FAKE_CALLS", str(tmp_path / "calls.jsonl"))
    import shutil
    # Only the fake CLIs, git and the OS: a real `claude`/`codex` on this machine must not leak in.
    system = [str(Path(shutil.which("git")).parent), "/usr/bin", "/bin"] if os.name != "nt" else \
        os.environ["PATH"].split(os.pathsep)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bindir), *dict.fromkeys(system)]))
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "proj"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('hi')\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")

    class Env:
        pass
    e = Env()
    e.tmp, e.bindir, e.repo, e.remote, e.monkeypatch = tmp_path, bindir, repo, remote, monkeypatch
    e.main_before = git(repo, "rev-parse", "main")
    e.remote_before = git(repo, "ls-remote", "origin")

    def install(*harnesses):
        for harness in harnesses:
            if os.name == "nt":
                (bindir / f"{harness}.cmd").write_text(
                    f'@"{sys.executable}" "{ROOT / "product_fake_cli.py"}" --as {harness} %*\r\n')
                continue
            wrapper = bindir / harness
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "product_fake_cli.py"}" '
                               f'--as {harness} "$@"\n')
            wrapper.chmod(0o755)
    e.install = install

    def scenario(data):
        path = tmp_path / "scenario.json"
        path.write_text(json.dumps(data))
        monkeypatch.setenv("AAW_FAKE_SCENARIO", str(path))
    e.scenario = scenario

    def calls():
        path = tmp_path / "calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
    e.calls = calls
    return e


def form(env, **extra):
    # These suites exercise the classic review-after-every-iteration lifecycle; chain mode has its own tests
    # (test_autonomy_chain.py) and the product switch is `advanced.chain_mode`.
    advanced = {"chain_mode": False, **extra.pop("advanced", {})}
    return {"repo": str(env.repo), "goal": "Build a tiny greeting module",
            "first_iteration": "Add greet(name) with a unit test",
            "directions": ["multi-language greetings"], "advanced": advanced, **extra}


def wait_for(run_id, predicate, timeout=90, interval=0.15):
    deadline = time.time() + timeout
    view = None
    while time.time() < deadline:
        view = pv.run_view(run_id)
        if predicate(view):
            return view
        time.sleep(interval)
    raise AssertionError(f"timeout; last status {view and view['status']}: "
                         f"{json.dumps((view or {}).get('worker_exit'))[:2000]}")


def settled(view):
    return view["status"] not in (pv.S_RUNNING, pv.S_STARTING, pv.S_STOPPING)


def assert_no_merge_push(env):
    assert git(env.repo, "rev-parse", "main") == env.main_before
    assert git(env.repo, "ls-remote", "origin") == env.remote_before
    assert git(env.repo, "status", "--porcelain") == ""


def start(env, **extra):
    detection = prun.detection_snapshot(refresh=True)
    return prun.start_task(form(env, **extra), detection=detection)["run_id"]


# ── provider detection ───────────────────────────────────────────────────────

def test_no_cli_found_blocks_start_with_setup_help(env, monkeypatch):
    monkeypatch.setattr(pp.wr, "harness_executable", lambda harness: None)
    detection = pp.detect_all()
    assert not detection["any_found"]
    assert {p["status"] for p in detection["providers"]} == {pp.NOT_FOUND}
    assert all(p["setup_help"]["install"] for p in detection["providers"])
    preview = prun.preview_task(form(env), detection=detection)
    assert not preview["can_start"]
    assert any("Nie znaleziono" in b for b in preview["blockers"])
    with pytest.raises(prun.ProductError):
        prun.start_task(form(env), detection=detection)


def test_only_claude(env):
    env.install("claude")
    detection = pp.detect_all()
    claude, codex, agy = detection["providers"]
    assert (claude["status"], claude["version"], claude["login"]) == (pp.FOUND, "9.9.9", pp.LOGGED_IN)
    assert codex["status"] == pp.NOT_FOUND and agy["status"] == pp.NOT_FOUND
    assert set(claude["runnable_profiles"]) == {"SONNET_HIGH", "OPUS_HIGH"}
    preview = prun.preview_task(form(env), detection=detection)
    assert preview["can_start"], preview["blockers"]
    assert preview["planner"]["profile_id"] == "OPUS_HIGH" and preview["planner"]["status"] == "ALTERNATIVE"
    assert preview["implementer_policy"]["implementer_default"]["profile_id"] == "SONNET_HIGH"
    # review stays on a different model than implementation
    assert preview["review_policy"]["primary_reviewer"]["runtime_model_id"] != \
        preview["implementer_policy"]["implementer_default"]["runtime_model_id"]


def test_only_codex_keeps_v03_policy(env):
    env.install("codex")
    detection = pp.detect_all()
    assert [p["status"] for p in detection["providers"]] == [pp.NOT_FOUND, pp.FOUND, pp.NOT_FOUND]
    preview = prun.preview_task(form(env), detection=detection)
    assert preview["can_start"], preview["blockers"]
    v03 = {"implementer_default": "GPT6_LUNA_HIGH", "implementer_harder": "GPT6_LUNA_VERY_HIGH",
           "implementer_hard": "GPT6_LUNA_MAX", "repair_default": "GPT6_LUNA_VERY_HIGH",
           "repair_hard": "GPT6_LUNA_MAX", "review_pretreatment": "GPT6_LUNA_VERY_HIGH"}
    for slot, profile in v03.items():
        assert preview["implementer_policy"][slot]["profile_id"] == profile
        assert preview["implementer_policy"][slot]["status"] == "RECOMMENDED"
    assert preview["review_policy"]["primary_reviewer"]["profile_id"] == "SOL_6_1_LIGHT"
    assert preview["review_policy"]["final_review_default"]["profile_id"] == "SOL_5_6_LIGHT"


def test_both_providers(env):
    env.install("claude", "codex")
    detection = pp.detect_all()
    assert all(p["status"] == pp.FOUND for p in detection["providers"] if p["harness"] in ("claude", "codex"))
    preview = prun.preview_task(form(env), detection=detection)
    assert preview["planner"]["profile_id"] == "OPUS_HIGH"           # Opus 5.5 unmapped → visible alternative
    # the system default implementer chain (Luna 6 → Terra 5.6 → Sonnet 5.5): Sonnet steps need a local probe
    chain = preview["implementer_chain"]
    assert chain["source"] == "DEFAULT"
    assert [s["profile_id"] for s in chain["steps"]] == [
        "GPT6_LUNA_HIGH", "GPT6_LUNA_VERY_HIGH", "GPT6_LUNA_MAX", "TERRA_HIGH", "TERRA_VERY_HIGH", "TERRA_MAX"]
    assert [s["profile_id"] for s in chain["skipped"]] == ["CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH"]
    assert preview["implementer_policy"]["implementer_capability_escalation"]["profile_id"] == "TERRA_HIGH"
    assert preview["implementer_policy"]["implementer_default"]["status"] == "RECOMMENDED"


def test_not_logged_in_blocks_and_explains(env, monkeypatch):
    env.install("claude")
    monkeypatch.setenv("AAW_FAKE_LOGGED_IN", "0")
    detection = pp.detect_all()
    assert detection["providers"][0]["login"] == pp.NOT_LOGGED_IN
    preview = prun.preview_task(form(env), detection=detection)
    assert not preview["can_start"]
    assert any("nie jest zalogowane" in b for b in preview["blockers"])


def test_login_parsers_never_guess():
    assert pp._claude_login(0, "garbage", "")[0] == pp.LOGIN_UNKNOWN
    assert pp._codex_login(127, "", "executable not found")[0] == pp.LOGIN_UNKNOWN
    assert pp._codex_login(0, "Logged in using ChatGPT", "")[0] == pp.LOGGED_IN


# ── recommendations ─────────────────────────────────────────────────────────

def test_builtin_catalog_recommended_equals_frozen_v03_policy():
    catalog = pr.builtin_catalog()
    frozen = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text())["policy_profiles"]
    slots = {}
    for group in pr.CHOICE_GROUPS:
        default = catalog["choices"][group]["default"]
        slots.update({k: v[0] for k, v in catalog["choices"][group]["options"][default]["slots"].items()})
    assert slots == frozen


def test_offline_update_does_not_block(env):
    def offline(url, timeout):
        raise OSError("network unreachable")
    result = pr.update_catalog(fetch=offline)
    assert result["status"] == "OFFLINE"
    assert pr.effective_catalog()["_source"] == "BUILTIN"
    env.install("claude")
    assert prun.preview_task(form(env), detection=pp.detect_all())["can_start"]


def test_update_newer_older_invalid(env):
    builtin = pr.builtin_catalog()
    newer = json.loads(json.dumps(builtin))
    newer["catalog_version"] = "2099.01.01.1"
    newer["choices"]["planning"]["options"]["STRONGEST"]["slots"]["initial_planner"] = ["SONNET_HIGH"]
    assert pr.update_catalog(fetch=lambda u, t: json.dumps(newer).encode())["status"] == "UPDATED"
    assert pr.effective_catalog()["catalog_version"] == "2099.01.01.1"
    assert pr.update_catalog(fetch=lambda u, t: json.dumps(builtin).encode())["status"] == "UP_TO_DATE"
    assert pr.update_catalog(fetch=lambda u, t: b'{"schema_version": "x"}')["status"] == "REJECTED"
    assert pr.update_catalog(fetch=lambda u, t: b"not json")["status"] == "OFFLINE"


def test_unavailable_recommended_profile_is_visible_and_required_slot_blocks():
    codex = pr.resolve_choices({"implementation": "BALANCED"}, runnable=CODEX_RUNNABLE)   # per-slot candidates, no chain
    assert codex["slots"]["initial_planner"]["status"] == "ALTERNATIVE"
    assert codex["slots"]["initial_planner"]["recommended_profile_id"] == "OPUS_5_5_HIGH"
    assert codex["implementer_chain"]["source"] == "SLOTS"
    assert codex["slots"]["implementer_capability_escalation"]["status"] == "UNAVAILABLE"
    assert not codex["blockers"]                       # optional escalation slot: warning only
    assert any("eskalacja" in w for w in codex["warnings"])
    nothing = pr.resolve_choices({}, runnable=set())
    assert nothing["blockers"]
    override = pr.resolve_choices({}, runnable=CODEX_RUNNABLE, overrides={"primary_reviewer": "OPUS_HIGH"})
    assert override["slots"]["primary_reviewer"]["status"] == "OVERRIDE"
    assert any("Review" in b for b in override["blockers"])


# ── repo readiness ──────────────────────────────────────────────────────────

def test_dirty_repo_and_plain_folder(env, tmp_path):
    (env.repo / "scratch.txt").write_text("x")
    info = prun.inspect_repo(str(env.repo))
    assert not info["ready"] and info["dirty_count"] == 1
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("a")
    info = prun.inspect_repo(str(plain))
    assert not info["is_git"] and info["can_init_git"]
    assert prun.init_git_repo(str(plain))["ready"]


# ── autonomous execution through the product ────────────────────────────────

def test_start_to_human_gate_with_briefs_timeline_and_no_merge_push(env):
    env.install("claude")
    run_id = start(env)
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE, view
    gate = view["gate"]
    assert gate["hold_reason"] == ac.HOLD_ROADMAP_EXHAUSTED
    assert len(gate["done"]) == 2 and gate["remaining"] == []
    assert gate["actions"]["accept"] and not gate["actions"]["accept_needs_early_end"]
    assert [t["label"] for t in view["timeline"]] == ["PASS", "PASS"]
    phases = [b["kind"] for b in view["timeline"][0]["briefs"]]
    assert phases == ["CHARTER", "PLAN", "EXECUTE", "SELF_VERIFY", "REVIEW", "FINAL_REVIEW"]
    for brief in view["timeline"][0]["briefs"]:
        assert {"phase", "model", "goal", "done", "checks", "problems", "handed_over"} <= set(brief)
    execute = view["timeline"][0]["briefs"][2]
    assert execute["execution_id"] and execute["ledger_state"] == "CLOSED"
    evidence = pv.evidence_view(run_id, execute["execution_id"])
    assert evidence["descriptor"]["execution_id"] == execute["execution_id"]
    assert evidence["result_artifact"]["result"]["summary"] == execute["done"][0]
    assert view["process"]["roadmap"] == {"done": 2, "total": 2}
    assert view["process"]["eta"] is None
    assert_no_merge_push(env)
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    assert state["main_merge_allowed"] is False
    # the task record is not a state authority: the engine never reads it
    assert "authority_note" in prun.load_task(run_id)


def test_visible_phase_transitions(env):
    env.install("claude")
    slow = {"sleep": 1.2}
    env.scenario({"IMPLEMENTER": [{**slow, "write_files": {"src/a.py": "A = 1\n"}, "output": {
        "summary": "done", "changed_files": ["src/a.py"], "checks": [], "deviations": [], "uncertainties": []}}],
        "REVIEWER": [{**slow, "output": {"verdict": "PASS", "summary": "ok", "findings": [],
                                         "raw_evidence_requests": [], "uncertainties": []}}],
        "FINAL REVIEWER": [{**slow, "output": {"verdict": "PASS", "summary": "ok", "findings": [],
                                               "raw_evidence_requests": [], "uncertainties": []}}]})
    run_id = start(env, directions=[])
    keys, models = [], {}
    deadline = time.time() + 60
    while time.time() < deadline:
        view = pv.run_view(run_id)
        process = view.get("process") or {}
        active = next((n["key"] for n in process.get("nodes", []) if n["state"] == "active"), None)
        if active and (not keys or keys[-1] != active):
            keys.append(active)
        if active and process.get("model"):
            models.setdefault(active, set()).add(process["model"])
        if settled(view):
            break
        time.sleep(0.1)
    assert keys.index("EXECUTE") < keys.index("REVIEW") < keys.index("FINAL_REVIEW"), keys
    assert {"EXECUTE", "REVIEW", "FINAL_REVIEW"} <= set(models), models   # the running model is shown
    assert view["status"] == pv.S_GATE


def test_repair_is_visible_in_timeline(env):
    env.install("claude")
    finding = {"finding_key": "F1", "severity": "HIGH", "summary": "missing edge case", "file": "src/a.py",
               "blocking": True, "evidence_ref": "RAW_DIFF", "finding_code": None}
    env.scenario({"REVIEWER": [
        {"output": {"verdict": "REPAIR_REQUIRED", "summary": "fix F1", "findings": [finding],
                    "raw_evidence_requests": [], "uncertainties": []}},
        {"output": {"verdict": "PASS", "summary": "fixed", "findings": [], "raw_evidence_requests": [],
                    "uncertainties": []}}],
        "REPAIRER": [{"write_files": {"src/fix.py": "FIX = 1\n"}, "output": {
            "summary": "handled the edge case", "addressed_findings": ["F1"], "changed_files": ["src/fix.py"],
            "checks": [], "uncertainties": []}}]})
    run_id = start(env, directions=[])
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE
    assert view["timeline"][0]["label"] == "REPAIR → PASS"
    kinds = [b["kind"] for b in view["timeline"][0]["briefs"]]
    assert kinds.count("REVIEW") == 2 and "REPAIR" in kinds
    assert kinds.index("REPAIR") < kinds.index("FINAL_REVIEW")
    assert any("missing edge case" in p for p in view["timeline"][0]["briefs"][kinds.index("REVIEW")]["problems"])


def _slow(role, seconds, output=None):
    return {role: [{"sleep": seconds, **({"output": output} if output else {})}]}


def test_stop_during_execute_waits_then_pauses_and_resume_completes(env):
    env.install("claude")
    env.scenario({"IMPLEMENTER": [{"sleep": 3, "write_files": {"src/a.py": "A = 1\n"}, "output": {
        "summary": "done", "changed_files": ["src/a.py"], "checks": [], "deviations": [], "uncertainties": []}}]})
    run_id = start(env, directions=[])
    wait_for(run_id, lambda v: (v.get("process") or {}).get("phase") == "EXECUTE"
             and (v["process"].get("execution_id")), timeout=30, interval=0.05)
    prun.request_stop(run_id)
    stopping = wait_for(run_id, lambda v: v["status"] == pv.S_STOPPING, timeout=10, interval=0.05)
    view = wait_for(run_id, settled, timeout=30)
    assert view["status"] == pv.S_PAUSED
    effect = json.loads((prun.product_dir(run_id) / "stop_effect.json").read_text())["events"][0]
    assert effect["mode"] == "PAUSE_AT_NEXT_BOUNDARY"      # the implementer was NOT killed
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    assert state["status"] == ac.RUNNING and state["in_flight"] is None and state["phase"] == "SELF_VERIFY"
    execute = [e for e in state["executions"] if e["executor"] == "execute"][0]
    assert execute["close_record_status"] == "RECORDED"
    assert any(b["kind"] == "PAUSED" for t in view["timeline"] for b in t["briefs"])
    assert view["controls"]["resume"] and not view["controls"]["stop"]
    assert stopping
    prun.resume_task(run_id)
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE, (view.get("status_detail"), view.get("worker_exit"))
    assert len([c for c in env.calls() if c["role"] == "IMPLEMENTER"]) == 1     # never replayed
    assert_no_merge_push(env)


def test_stop_during_review_cancels_read_only_call_and_resume_replays_with_new_id(env):
    env.install("claude")
    env.scenario({"REVIEWER": [{"sleep": 30}, {"output": {"verdict": "PASS", "summary": "ok", "findings": [],
                                                           "raw_evidence_requests": [], "uncertainties": []}}]})
    run_id = start(env, directions=[])
    wait_for(run_id, lambda v: (v.get("process") or {}).get("phase") == "REVIEW"
             and v["process"].get("execution_id"), timeout=30, interval=0.05)
    time.sleep(0.5)
    started = time.time()
    prun.request_stop(run_id)
    view = wait_for(run_id, settled, timeout=20)
    assert time.time() - started < 15                     # the 30 s review was cancelled, not awaited
    assert view["status"] == pv.S_PAUSED
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    flight = state["in_flight"]
    assert flight and flight["phase"] == "REVIEW"
    cancelled_id = flight["execution_id"]
    lifecycle = pv._ledger(run_id)[cancelled_id]
    assert lifecycle["closed"][-1]["payload"]["close_reason"] == "CANCELLED"
    assert any(b["kind"] == "CANCELLED" for t in view["timeline"] for b in t["briefs"])
    prun.resume_task(run_id)
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE, (view.get("status_detail"), view.get("worker_exit"))
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    reviews = [e for e in state["executions"] if e["executor"] == "review"]
    assert reviews[-1]["retry_of_execution_id"] == cancelled_id
    assert reviews[-1]["execution_id"] != cancelled_id


def test_force_stop_during_execute_is_never_replayed_blindly(env):
    env.install("claude")
    env.scenario({"IMPLEMENTER": [{"sleep": 30}]})
    run_id = start(env, directions=[])
    wait_for(run_id, lambda v: (v.get("process") or {}).get("phase") == "EXECUTE"
             and v["process"].get("execution_id"), timeout=30, interval=0.05)
    time.sleep(0.5)
    prun.request_stop(run_id, force=True)
    view = wait_for(run_id, settled, timeout=20)
    assert view["status"] == pv.S_PAUSED
    assert any("NIEPEWNE" in p for t in view["timeline"] for b in t["briefs"] for p in b["problems"])
    prun.resume_task(run_id)
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_ATTENTION
    assert view["gate"]["escalation_code"] == ac.E_INTERRUPTED
    assert not view["gate"]["actions"]["accept"]
    assert any("przerwane przez STOP" in w for w in view["gate"]["warnings"])
    assert len([c for c in env.calls() if c["role"] == "IMPLEMENTER"]) == 1


def test_restart_after_worker_crash_resumes_through_lock_reconciliation(env):
    env.install("claude")
    env.scenario({"REVIEWER": [{"sleep": 30}, {"output": {"verdict": "PASS", "summary": "ok", "findings": [],
                                                           "raw_evidence_requests": [], "uncertainties": []}}]})
    run_id = start(env, directions=[])
    wait_for(run_id, lambda v: (v.get("process") or {}).get("phase") == "REVIEW"
             and v["process"].get("execution_id"), timeout=30, interval=0.05)
    deadline = time.time() + 30   # the provider call itself must have started (slow process start on Windows)
    while not any(c["role"] == "REVIEWER" for c in env.calls()) and time.time() < deadline:
        time.sleep(0.05)
    worker = json.loads((prun.product_dir(run_id) / "worker.json").read_text())
    os.kill(worker["pid"], getattr(signal, "SIGKILL", signal.SIGTERM))  # power loss / app crash
    view = wait_for(run_id, lambda v: v["status"] == pv.S_INTERRUPTED, timeout=15)
    assert view["controls"]["resume"] and view["controls"]["lock_token"]
    result = prun.resume_task(run_id, expected_lock_token=view["controls"]["lock_token"])
    assert result["reconciled_lock"]
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE, (view.get("gate") or {}).get("warnings")
    retired = list(prun.autonomy_dir(run_id).glob("controller.lock.retired.*.json"))
    assert retired and "AAW Product RESUME" in json.loads(retired[0].read_text())["operator"]


def test_accept_reject_and_continue_never_merge_or_push(env):
    env.install("claude")
    run_id = start(env, directions=[])
    wait_for(run_id, settled)
    decision = prun.accept(run_id)
    assert decision["integration"] == {"status": "READY_FOR_EXTERNAL_INTEGRATION", "merged": False, "pushed": False}
    view = pv.run_view(run_id)
    assert view["status"] == pv.S_ACCEPTED and view["section"] == "completed"
    assert_no_merge_push(env)
    cont = prun.prepare_continuation(run_id, mode="direction")
    base = cont["base"]
    task = prun.load_task(run_id)
    assert base["branch"] == task["workspace"]["branch"]
    assert git(task["workspace"]["worktree"], "rev-parse", "HEAD") == base["commit"]
    assert_no_merge_push(env)
    prefill = cont["prefill"]
    assert prefill["advanced"]["chain_mode"] is False           # the follow-up keeps the mode of the run it continues
    prefill.update(first_iteration="Add a farewell function", directions=[])
    follow_up = prun.start_task(prefill, detection=prun.detection_snapshot())["run_id"]
    view = wait_for(follow_up, settled)
    assert view["status"] == pv.S_GATE
    assert prun.load_task(follow_up)["workspace"]["base_commit"] == base["commit"]
    prun.reject(follow_up, reason="not needed")
    assert pv.run_view(follow_up)["status"] == pv.S_REJECTED
    assert_no_merge_push(env)


def test_iteration_cap_needs_explicit_early_end(env):
    env.install("claude")
    run_id = start(env, directions=["second", "third"], advanced={"max_iterations": 1})
    view = wait_for(run_id, settled)
    assert view["gate"]["hold_reason"] == ac.HOLD_ITERATION_CAP
    assert view["gate"]["actions"]["accept_needs_early_end"]
    assert len(view["gate"]["remaining"]) == 2
    with pytest.raises(prun.ProductError):
        prun.accept(run_id, early_end=False)
    prun.accept(run_id, early_end=True)
    assert_no_merge_push(env)


def test_unavailable_escalation_profile_stops_fail_closed_at_human_gate(env):
    """Codex-only: a final review that needs the 'hard' tier finds Sonnet 5.5 unmapped → Human Gate, no substitute."""
    env.install("codex")
    env.scenario({"IMPLEMENTER": [{"write_files": {"src/a.py": "A = 1\n"}, "output": {
        "summary": "done", "changed_files": ["src/a.py"], "checks": [], "deviations": [],
        "uncertainties": ["environment detail could not be verified"]}}]})
    run_id = start(env, directions=[], advanced={"profile_overrides": {"final_review_hard": "SONNET_5_5_MEDIUM"}})
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_ATTENTION
    assert view["gate"]["escalation_code"] == ac.E_ROLE_UNAVAILABLE
    assert all(c["harness"] == "codex" for c in env.calls())


def test_running_run_keeps_its_bindings_after_catalog_update(env):
    env.install("claude")
    env.scenario({"IMPLEMENTER": [{"sleep": 3, "write_files": {"src/a.py": "A = 1\n"}, "output": {
        "summary": "done", "changed_files": ["src/a.py"], "checks": [], "deviations": [], "uncertainties": []}}]})
    run_id = start(env, directions=[])
    wait_for(run_id, lambda v: (v.get("process") or {}).get("phase") == "EXECUTE", timeout=30, interval=0.05)
    newer = pr.builtin_catalog()
    newer["catalog_version"] = "2099.01.01.1"
    newer["choices"]["review"]["options"]["RECOMMENDED"]["slots"]["primary_reviewer"] = ["SONNET_HIGH"]
    assert pr.update_catalog(fetch=lambda u, t: json.dumps(newer).encode())["status"] == "UPDATED"
    wait_for(run_id, settled)
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    reviews = [e for e in state["executions"] if e["executor"] == "review"]
    assert reviews and all(e["profile"] == "OPUS_HIGH" for e in reviews)
    # a NEW run uses the updated catalog
    preview = prun.preview_task(form(env), detection=prun.detection_snapshot())
    assert preview["review_policy"]["primary_reviewer"]["profile_id"] == "SONNET_HIGH"


# ── home & server ───────────────────────────────────────────────────────────

def test_home_sections(env):
    env.install("claude")
    run_id = start(env, directions=[])
    wait_for(run_id, settled)
    home = pv.home_view()
    card = home["sections"]["attention"][0]
    assert card["run_id"] == run_id and card["project"] == "proj" and card["goal"]
    assert {"iteration", "phase", "last_activity", "status"} <= set(card)


def test_server_requires_token_and_loopback_host(env):
    import threading
    import product_server
    port = product_server.free_port()
    ready = threading.Event()
    thread = threading.Thread(target=product_server.serve, kwargs={"port": port, "open_browser": False,
                                                                  "ready": lambda url: ready.set()}, daemon=True)
    thread.start()
    assert ready.wait(10)
    token = json.loads((product_home.home() / "ui.json").read_text())["token"]
    base = f"http://127.0.0.1:{port}"

    def call(path, headers=None, data=None):
        request = urllib.request.Request(base + path, headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()
    assert call("/api/home")[0] == 403
    assert call("/api/home", {"X-AAW-Token": "wrong"})[0] == 403
    assert call("/api/home", {"X-AAW-Token": token, "Host": "evil.example"})[0] == 403
    status, body = call("/api/home", {"X-AAW-Token": token})
    assert status == 200 and "sections" in json.loads(body)
    status, page = call("/")
    assert status == 200 and token in page
    assert call("/static/../product_runs.py")[0] == 404
    call("/api/quit", {"X-AAW-Token": token, "Content-Type": "application/json"}, b"{}")
    thread.join(10)
