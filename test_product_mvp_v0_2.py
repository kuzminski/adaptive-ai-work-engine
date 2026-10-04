"""AAW Product MVP V0.2 — release-hardening checks (provider probes, exact model mappings,
project readiness, Home, server endpoints, release package).

Same real stack as `test_product_mvp.py` (real worker processes, real V0.3 engine,
`product_fake_cli.py` standing in for the AI CLIs) and the same fixtures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import product_home  # noqa: E402
import product_providers as pp  # noqa: E402
import product_recommendations as pr  # noqa: E402
import product_runs as prun  # noqa: E402
import product_version  # noqa: E402
import product_view as pv  # noqa: E402
from test_product_mvp import assert_no_merge_push, env, form, git, settled, start, wait_for  # noqa: E402,F401

EXACT = {"OPUS_5_5_HIGH": "CLAUDE_OPUS_5_5_HIGH", "OPUS_5_5_MEDIUM": "CLAUDE_OPUS_5_5_MEDIUM",
         "SONNET_5_5_MEDIUM": "CLAUDE_SONNET_5_5_MEDIUM"}


# ── probe classification: never guesses ACCEPTED ─────────────────────────────

def test_probe_classifier_is_conservative():
    ok = json.dumps({"type": "result", "is_error": False, "result": pp.PROBE_TOKEN})
    assert pp.classify_probe("claude", 0, ok, "")[0] == pp.PROBE_ACCEPTED
    rejected = json.dumps({"is_error": True, "api_error_status": 404, "result": "There's an issue with the "
                           "selected model (x). It may not exist or you may not have access to it."})
    assert pp.classify_probe("claude", 1, rejected, "[claude-code:unrecognized_model]")[0] == pp.PROBE_REJECTED
    assert pp.classify_probe("codex", 1, "", "ERROR: The model `gpt-x` does not exist or you do not have access "
                                             "to it.")[0] == pp.PROBE_REJECTED
    assert pp.classify_probe("codex", 0, pp.PROBE_TOKEN + "\n", "")[0] == pp.PROBE_ACCEPTED
    # offline, overloaded, logged out, timeout, error JSON with the token inside → never ACCEPTED, never REJECTED
    assert pp.classify_probe("claude", 1, "", "API Error: Connection error (getaddrinfo ENOTFOUND)")[0] == pp.PROBE_UNKNOWN
    assert pp.classify_probe("claude", 1, "", "model is overloaded, please retry")[0] == pp.PROBE_UNKNOWN
    assert pp.classify_probe("codex", 1, "", "Not logged in")[0] == pp.PROBE_UNKNOWN
    assert pp.classify_probe("claude", 124, "", "timed out")[0] == pp.PROBE_UNKNOWN
    error_with_token = json.dumps({"is_error": True, "result": pp.PROBE_TOKEN})
    assert pp.classify_probe("claude", 0, error_with_token, "")[0] != pp.PROBE_ACCEPTED
    assert pp.classify_probe("claude", 0, "garbage", "")[0] == pp.PROBE_UNKNOWN


def test_probe_argv_is_minimal_and_never_writes():
    claude = pp.probe_argv("claude", "claude", "claude-opus-5-5")
    assert claude[claude.index("--model") + 1] == "claude-opus-5-5"
    assert "--tools" in claude and "--permission-mode" in claude and claude[claude.index("--permission-mode") + 1] == "plan"
    assert "--max-turns" in claude and claude[claude.index("--max-turns") + 1] == "1"
    codex = pp.probe_argv("codex", "codex", "gpt-6-luna")
    assert codex[codex.index("--sandbox") + 1] == "read-only" and "--ephemeral" in codex


# ── frozen catalog untouched; exact mappings are consistent ──────────────────

def test_frozen_v03_profiles_unchanged_and_exact_mappings_are_the_same_model_and_effort():
    profiles = {r["profile_id"]: r for r in json.loads((ROOT / "IMPLEMENTER_PROFILES.json").read_text())["profiles"]}
    models = {r["runtime_model_id"]: r for r in json.loads((ROOT / "MODEL_CATALOG.json").read_text())["models"]}
    assert pr.exact_mappings(pr.builtin_catalog()) == EXACT
    for frozen, exact in EXACT.items():
        assert profiles[frozen]["availability"] == "KNOWN_BUT_UNAVAILABLE"
        assert "runtime_model_id" not in profiles[frozen]
        mapped = profiles[exact]
        assert mapped["exact_runtime_mapping_of"] == frozen
        assert mapped["effort"] == profiles[frozen]["effort"] and mapped["harness"] == profiles[frozen]["harness"]
        assert mapped["availability_policy"] == pp.PROBE_REQUIRED
        family = profiles[frozen]["display_name"].split(" / ")[0]           # "Claude Opus 5.5"
        assert models[mapped["runtime_model_id"]]["model_family"] == family
        assert models[mapped["runtime_model_id"]]["runtime_available_policy"] == "DYNAMIC_PREFLIGHT"
    # the default levels still start with the frozen V0.3 profiles (exact mappings are not candidates)
    frozen_policy = json.loads((ROOT / "AUTONOMY_ROLES.json").read_text())["policy_profiles"]
    catalog = pr.builtin_catalog()
    for group in pr.CHOICE_GROUPS:
        default = catalog["choices"][group]["options"][catalog["choices"][group]["default"]]["slots"]
        for slot, candidates in default.items():
            assert candidates[0] == frozen_policy[slot]
            assert not set(candidates) & set(EXACT.values())


# ── availability = catalog policy + local runtime + probe evidence ──────────

def test_claude_only_without_probe_uses_visible_alternative_and_offers_check(env):
    env.install("claude")
    detection = prun.detection_snapshot(refresh=True)
    states = pp.profile_states(detection)
    assert states["CLAUDE_OPUS_5_5_HIGH"]["state"] == pp.A_NEEDS_CHECK and not states["CLAUDE_OPUS_5_5_HIGH"]["runnable"]
    assert states["OPUS_5_5_HIGH"]["state"] == pp.A_POLICY
    assert states["FABLE_HIGH"]["state"] == pp.A_POLICY
    assert states["OPUS_HIGH"]["state"] == pp.A_AVAILABLE
    assert states["GPT6_LUNA_HIGH"]["state"] == pp.A_CLI_MISSING
    preview = prun.preview_task(form(env), detection=detection)
    planner = preview["planner"]
    assert planner["profile_id"] == "OPUS_HIGH" and planner["status"] == "ALTERNATIVE"
    assert planner["recommended_check_profile"] == "CLAUDE_OPUS_5_5_HIGH"
    assert "Sprawdź modele" in planner["reason"]
    assert "CLAUDE_OPUS_5_5_HIGH" in preview["groups"]["planning"]["checkable"]


def test_verify_models_activates_exact_mapping_and_run_uses_exact_model_id(env):
    env.install("claude")
    prun.detection_snapshot(refresh=True)
    result = prun.verify_models(["CLAUDE_OPUS_5_5_HIGH", "CLAUDE_OPUS_5_5_MEDIUM", "CLAUDE_SONNET_5_5_MEDIUM"])
    assert {(r["model"], r["status"]) for r in result["results"]} == {
        ("claude-opus-5-5", pp.PROBE_ACCEPTED), ("claude-sonnet-5-5", pp.PROBE_ACCEPTED)}
    probes = [c for c in env.calls() if c["role"] == "MODEL_PROBE"]
    assert len(probes) == 2                       # one per distinct model, not per profile
    preview = prun.preview_task(form(env), detection=prun.detection_snapshot())
    planner = preview["planner"]
    assert (planner["profile_id"], planner["status"], planner["exact_mapping_of"]) == (
        "CLAUDE_OPUS_5_5_HIGH", "RECOMMENDED", "OPUS_5_5_HIGH")
    assert planner["runtime_model_id"] == "claude-opus-5-5" and planner["effort"] == "high"
    assert preview["review_policy"]["final_review_critical"]["profile_id"] == "CLAUDE_OPUS_5_5_MEDIUM"
    # Claude-only after verification: the system default chain keeps its Sonnet 5.5 steps, in order.
    chain = preview["implementer_chain"]
    assert chain["source"] == "DEFAULT"
    assert [s["profile_id"] for s in chain["steps"]] == ["CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH"]
    assert preview["implementer_policy"]["implementer_default"]["profile_id"] == "CLAUDE_SONNET_5_5_MEDIUM"
    assert preview["implementer_policy"]["implementer_capability_escalation"]["profile_id"] == \
        "CLAUDE_SONNET_5_5_HIGH"
    run_id = start(env, directions=[])
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    initial = [e for e in state["executions"] if e["role"] == "initial_planner"]
    assert len(initial) == 1 and initial[0]["profile"] == "CLAUDE_OPUS_5_5_HIGH"
    planner_call = next(c for c in env.calls() if c["role"] == "PLANNER")
    assert planner_call["argv"][planner_call["argv"].index("--model") + 1] == "claude-opus-5-5"
    assert planner_call["argv"][planner_call["argv"].index("--effort") + 1] == "high"
    assert_no_merge_push(env)


def test_rejected_probe_keeps_mapping_unavailable_and_says_why(env, monkeypatch):
    env.install("claude")
    monkeypatch.setenv("AAW_FAKE_REJECT_MODELS", "claude-opus-5-5")
    prun.detection_snapshot(refresh=True)
    prun.verify_models(["CLAUDE_OPUS_5_5_HIGH"])
    detection = prun.detection_snapshot()
    state = pp.profile_states(detection)["CLAUDE_OPUS_5_5_HIGH"]
    assert state["state"] == pp.A_REJECTED_HERE and not state["runnable"]
    planner = prun.preview_task(form(env), detection=detection)["planner"]
    assert planner["profile_id"] == "OPUS_HIGH" and planner["status"] == "ALTERNATIVE"
    assert "odrzuciło" in planner["reason"]


def test_offline_probe_changes_nothing_and_keeps_earlier_evidence(env, monkeypatch):
    env.install("claude")
    prun.detection_snapshot(refresh=True)
    monkeypatch.setenv("AAW_FAKE_OFFLINE", "1")
    first = prun.verify_models(["CLAUDE_OPUS_5_5_HIGH"])
    assert first["results"][0]["status"] == pp.PROBE_UNKNOWN
    assert pp.profile_states(prun.detection_snapshot())["CLAUDE_OPUS_5_5_HIGH"]["state"] == pp.A_NEEDS_CHECK
    monkeypatch.setenv("AAW_FAKE_OFFLINE", "0")
    prun.verify_models(["CLAUDE_OPUS_5_5_HIGH"])
    assert pp.profile_states(prun.detection_snapshot())["CLAUDE_OPUS_5_5_HIGH"]["state"] == pp.A_VERIFIED_HERE
    monkeypatch.setenv("AAW_FAKE_OFFLINE", "1")
    again = prun.verify_models(["CLAUDE_OPUS_5_5_HIGH"])
    assert again["results"][0]["status"] == pp.PROBE_UNKNOWN and again["results"][0]["previous"]["status"] == \
        pp.PROBE_ACCEPTED
    assert pp.profile_states(prun.detection_snapshot())["CLAUDE_OPUS_5_5_HIGH"]["state"] == pp.A_VERIFIED_HERE
    # START still works offline with the evidence already on disk; detection itself needs no network
    assert prun.preview_task(form(env), detection=prun.detection_snapshot())["can_start"]


def test_probe_evidence_expires_when_the_cli_version_changes(env, monkeypatch):
    env.install("claude")
    prun.detection_snapshot(refresh=True)
    prun.verify_models(["CLAUDE_OPUS_5_5_HIGH"])
    assert pp.profile_states(prun.detection_snapshot())["CLAUDE_OPUS_5_5_HIGH"]["state"] == pp.A_VERIFIED_HERE
    monkeypatch.setenv("AAW_FAKE_VERSION", "10.0.0")
    detection = prun.detection_snapshot(refresh=True)
    assert pp.profile_states(detection)["CLAUDE_OPUS_5_5_HIGH"]["state"] == pp.A_NEEDS_CHECK


def test_codex_dynamic_models_are_usable_but_flagged_until_verified(env, monkeypatch):
    env.install("codex")
    detection = prun.detection_snapshot(refresh=True)
    assert pp.profile_states(detection)["GPT6_LUNA_HIGH"]["state"] == pp.A_NOT_VERIFIED
    preview = prun.preview_task(form(env), detection=detection)
    assert preview["can_start"]
    assert any("Jeszcze nie sprawdzono" in w for w in preview["warnings"])
    monkeypatch.setenv("AAW_FAKE_REJECT_MODELS", "gpt-6-luna")
    prun.verify_models(["GPT6_LUNA_HIGH"])
    detection = prun.detection_snapshot()
    assert pp.profile_states(detection)["GPT6_LUNA_HIGH"]["state"] == pp.A_REJECTED_HERE
    preview = prun.preview_task(form(env), detection=detection)
    implementer = preview["implementer_policy"]["implementer_default"]
    # never substituted: the required slot is shown UNAVAILABLE and START is blocked with a way out
    assert implementer["status"] == "UNAVAILABLE" and not preview["can_start"]
    assert any("Implementacja" in b and "inny poziom" in b for b in preview["blockers"])
    economic = prun.preview_task(form(env, implementation="ECONOMIC"), detection=detection)
    assert economic["implementer_policy"]["implementer_default"]["profile_id"] == "TERRA_HIGH"


def test_both_providers_detection_summary(env):
    env.install("claude", "codex")
    detection = prun.detection_snapshot(refresh=True)
    assert detection["any_ready"] and detection["setup_notice"]
    for provider in detection["providers"]:
        assert provider["status"] == pp.FOUND and provider["version"] == "9.9.9" and provider["login"] == pp.LOGGED_IN
        assert provider["models"] and all("state" in m and "label" in m for m in provider["models"])
        assert provider["setup_help"]["docs"].startswith("https://")


# ── project readiness (never modifies the user's checkout) ───────────────────

def test_project_readiness_messages(env, tmp_path, monkeypatch):
    ready = prun.inspect_repo(str(env.repo))
    assert ready["ready"] and "izolowanej" in ready["message"] and ready["action"] is None
    sub = env.repo / "pkg"
    sub.mkdir()
    (sub / "keep.txt").write_text("x")
    git(env.repo, "add", ".")
    git(env.repo, "commit", "-qm", "sub")
    env.main_before = git(env.repo, "rev-parse", "main")
    info = prun.inspect_repo(str(sub))
    assert info["ready"] and info["top"] == str(env.repo) and "podfolder" in info["subfolder_note"]
    # untracked + modified: full file names (V0.1 cut the first character), exact action
    (env.repo / "app.py").write_text("print('changed')\n")
    (env.repo / "new.txt").write_text("n")
    dirty = prun.inspect_repo(str(env.repo))
    assert not dirty["ready"] and dirty["action"] == "COMMIT_OR_STASH"
    assert sorted(dirty["dirty_files"]) == ["app.py", "new.txt"] and dirty["untracked_count"] == 1
    assert git(env.repo, "status", "--porcelain") != ""            # untouched: AAW never cleans it
    git(env.repo, "checkout", "--", "app.py")
    (env.repo / "new.txt").unlink()
    # bare repository
    bare = prun.inspect_repo(str(env.remote))
    assert not bare["ready"] and "gołe" in bare["message"]
    # the AAW data folder is not a project
    product_home.worktrees_root()
    assert "folder roboczy AAW" in prun.inspect_repo(str(product_home.home()))["message"]
    # merge in progress
    gitdir = Path(git(env.repo, "rev-parse", "--absolute-git-dir"))
    (gitdir / "MERGE_HEAD").write_text(git(env.repo, "rev-parse", "HEAD") + "\n")
    merging = prun.inspect_repo(str(env.repo))
    assert not merging["ready"] and merging["action"] == "FINISH_GIT_OPERATION"
    (gitdir / "MERGE_HEAD").unlink()
    # detached HEAD is fine (AAW starts from that commit)
    git(env.repo, "checkout", "-q", "--detach")
    detached = prun.inspect_repo(str(env.repo))
    assert detached["ready"] and detached["detached"] and "odłączony HEAD" in detached["message"]
    git(env.repo, "checkout", "-q", "main")
    # no git installed → plain explanation, no crash
    monkeypatch.setattr(prun, "_git_available", lambda: False)
    nogit = prun.inspect_repo(str(env.repo))
    assert not nogit["ready"] and nogit["action"] == "INSTALL_GIT"


# ── Home ─────────────────────────────────────────────────────────────────────

def test_home_order_and_paused_runs_offer_resume(env):
    env.install("claude")
    env.scenario({"IMPLEMENTER": [{"sleep": 2, "write_files": {"src/a.py": "A = 1\n"}, "output": {
        "summary": "done", "changed_files": ["src/a.py"], "checks": [], "deviations": [], "uncertainties": []}}]})
    run_id = start(env, directions=[])
    wait_for(run_id, lambda v: (v.get("process") or {}).get("phase") == "EXECUTE", timeout=30, interval=0.05)
    prun.request_stop(run_id)
    wait_for(run_id, settled)
    home = pv.home_view()
    assert list(home["sections"]) == ["running", "paused", "attention", "completed"]
    card = home["sections"]["paused"][0]
    assert card["run_id"] == run_id and card["can_resume"] and card["short_goal"]
    assert {"project", "iteration", "phase", "last_activity", "status", "status_label"} <= set(card)
    prun.resume_task(run_id, expected_lock_token=card["lock_token"])
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE
    assert pv.home_view()["sections"]["attention"][0]["run_id"] == run_id
    assert view["process"]["phase_label"] is None or isinstance(view["process"]["phase_label"], str)
    assert view["gate"]["accept_meaning"].startswith("Akceptuj = READY_FOR_EXTERNAL_INTEGRATION")
    assert_no_merge_push(env)


# ── server endpoints used by the wizard ─────────────────────────────────────

def test_wizard_endpoints(env):
    import product_server
    env.install("claude")
    port = product_server.free_port()
    ready = threading.Event()
    thread = threading.Thread(target=product_server.serve, kwargs={"port": port, "open_browser": False,
                                                                  "ready": lambda url: ready.set()}, daemon=True)
    thread.start()
    assert ready.wait(10)
    token = json.loads((product_home.home() / "ui.json").read_text())["token"]

    def call(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers={
            "X-AAW-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    try:
        boot = call("/api/bootstrap")
        assert boot["release"]["release"] == product_version.RELEASE and boot["setup_notice"]
        assert boot["settings"]["first_run_completed"] is False and boot["has_runs"] is False
        assert call("/api/version")["release"] == product_version.RELEASE
        call("/api/providers/detect", {})
        setup = call("/api/setup/resolve", {"choices": {}})
        assert set(setup["groups"]) == {"planning", "implementation", "review"}
        assert setup["groups"]["planning"]["models"] == ["Opus / high"]
        verified = call("/api/models/verify", {"choices": {}})
        assert {r["model"] for r in verified["results"]} >= {"claude-opus-5-5"}
        assert verified["setup"]["groups"]["planning"]["models"] == ["Claude Opus 5.5 / high (exact)"]
        assert call("/api/first-run/done", {})["first_run_completed"] is True
        assert call("/api/bootstrap")["settings"]["first_run_completed"] is True
    finally:
        try:
            call("/api/quit", {})
        except urllib.error.URLError:
            pass
        thread.join(10)


def test_read_json_survives_a_concurrent_atomic_replace(tmp_path, monkeypatch):
    """Windows: reading while the writer replaces the file raises PermissionError for a moment."""
    target = tmp_path / "autonomy_state.json"
    product_home.write_json(target, {"status": "AWAITING_HUMAN"})
    real_read_text, failures = Path.read_text, []

    def flaky_read_text(self, *args, **kwargs):
        if self == target and len(failures) < 3:
            failures.append(1)
            raise PermissionError(13, "The process cannot access the file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    assert product_home.read_json(target) == {"status": "AWAITING_HUMAN"} and len(failures) == 3
    assert product_home.read_json(tmp_path / "missing.json", "fallback") == "fallback"


# ── release package and entry point ─────────────────────────────────────────

def test_release_documents_and_version_entry_point():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_portable", ROOT / "packaging" / "build_portable.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    sources = {"QUICK_START.md": ROOT / "QUICK_START.md", "README.md": ROOT / "release" / "README.md",
               "CHANGELOG.md": ROOT / "CHANGELOG.md", "LICENSE": ROOT / "LICENSE",
               "SZYBKI_START.txt": ROOT / "SZYBKI_START.txt",
               "VERSION.txt": ROOT / "product_version.py"}                # generated from it at build time
    for name in build.REQUIRED_FILES:
        source = sources.get(name) or ROOT / "release" / "examples" / name.split("/", 1)[1]
        assert source.is_file(), name
    quick = (ROOT / "QUICK_START.md").read_text(encoding="utf-8")
    for needle in ("Download", "Unzip", "AAW.exe", "Run anyway", "START", "not part of AAW",
                   "READY_FOR_EXTERNAL_INTEGRATION", "never merges or pushes"):
        assert needle in quick, needle
    assert len(quick.splitlines()) < 60                                   # roughly one page
    assert product_version.RELEASE in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    out = subprocess.run([sys.executable, str(ROOT / "AAW.py"), "--version"], capture_output=True, text=True,
                         env={**os.environ, "AAW_PRODUCT_HOME": str(Path(spec.origin).parent.parent / "build" /
                                                                    "version_home")}, timeout=60)
    assert out.returncode == 0 and json.loads(out.stdout)["release"] == product_version.RELEASE


def test_release_zip_layout_and_start_guide_paths(tmp_path):
    """The ZIP a user downloads: one AAW/ folder, AAW.exe next to SZYBKI_START.txt, guides name only real paths."""
    import importlib.util
    import zipfile
    spec = importlib.util.spec_from_file_location("check_release_zip", ROOT / "packaging" / "check_release_zip.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    files = {"AAW.exe": b"stub", "_internal/base_library.zip": b"stub", "VERSION.txt": b"stub",
             "SZYBKI_START.txt": (ROOT / "SZYBKI_START.txt").read_bytes(),
             "QUICK_START.md": (ROOT / "QUICK_START.md").read_bytes(),
             "README.md": (ROOT / "release" / "README.md").read_bytes(),
             "CHANGELOG.md": (ROOT / "CHANGELOG.md").read_bytes(), "LICENSE": (ROOT / "LICENSE").read_bytes()}
    files.update({f"EXAMPLES/{p.name}": p.read_bytes() for p in (ROOT / "release" / "examples").iterdir()})
    archive = tmp_path / "AAW-Windows-x64.zip"
    with zipfile.ZipFile(archive, "w") as zf:                        # same layout as build_portable.py
        for name, data in files.items():
            zf.writestr(f"AAW/{name}", data)
    assert checker.check(str(archive)) == []
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("stray.txt", b"x")
    assert any("exactly one folder" in problem for problem in checker.check(str(archive)))
    szybki = (ROOT / "SZYBKI_START.txt").read_text(encoding="utf-8")
    for needle in ("releases/latest", "AAW-Windows-x64.zip", "AAW.exe", "Wyodrębnij", "START"):
        assert needle in szybki, needle


def test_self_test_checks_exact_mapping_consistency(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / "AAW.py"), "--self-test"], capture_output=True, text=True,
                         env={**os.environ, "AAW_PRODUCT_HOME": str(tmp_path)}, timeout=120)
    report = json.loads(out.stdout)
    assert out.returncode == 0 and report["status"] == "PASS" and report["checks"]["exact_mappings"]
    assert report["release"] == product_version.RELEASE


def test_no_cli_case_explains_setup_in_plain_language(env, monkeypatch):
    monkeypatch.setattr(pp.wr, "harness_executable", lambda harness: None)
    detection = pp.detect_all()
    assert not detection["any_found"] and not detection["any_ready"]
    for provider in detection["providers"]:
        help_ = provider["setup_help"]
        assert help_["install"] and help_["login"] and help_["verify"] and help_["docs"]
        assert all(m["state"] == pp.A_CLI_MISSING for m in provider["models"])
    assert pp.runnable_profiles(detection) == set()
    setup = prun.resolve_setup({}, detection=detection)
    assert setup["blockers"]                                           # START impossible, said plainly


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_not_logged_in_provider_has_no_usable_models(env, monkeypatch, harness):
    env.install(harness)
    monkeypatch.setenv("AAW_FAKE_LOGGED_IN", "0")
    detection = prun.detection_snapshot(refresh=True)
    provider = next(p for p in detection["providers"] if p["harness"] == harness)
    assert provider["login"] == pp.NOT_LOGGED_IN and provider["runnable_profiles"] == []
    assert all(m["state"] in (pp.A_LOGGED_OUT, pp.A_POLICY) for m in provider["models"])
    assert not detection["any_ready"]


# ── implementer chain chosen at start ────────────────────────────────────────

def test_user_can_pick_any_single_model_as_the_whole_implementer(env):
    env.install("codex")
    detection = prun.detection_snapshot(refresh=True)
    preview = prun.preview_task(form(env, implementer_chain=["TERRA_HIGH"]), detection=detection)
    assert preview["can_start"], preview["blockers"]
    assert preview["implementer_chain"]["source"] == "USER"
    assert [s["profile_id"] for s in preview["implementer_chain"]["steps"]] == ["TERRA_HIGH"]
    run_id = start(env, directions=[], implementer_chain=["TERRA_HIGH"])
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    assert [b["profile_id"] for b in state["roles"]["implementer_chain"]] == ["TERRA_HIGH"]   # frozen with the run
    impl = [e for e in state["executions"] if e["executor"] in ("execute", "self_verify")]
    assert impl and {e["profile"] for e in impl} == {"TERRA_HIGH"}
    assert json.loads((prun.product_dir(run_id) / "task.json").read_text())["form"]["implementer_chain"] == ["TERRA_HIGH"]
    implementer_call = next(c for c in env.calls() if c["role"] == "IMPLEMENTER")
    assert "gpt-5.6-terra" in implementer_call["argv"]                 # the CLI really got the chosen exact model
    assert_no_merge_push(env)


def test_default_run_freezes_the_system_chain_and_starts_on_its_first_step(env):
    env.install("codex")
    run_id = start(env, directions=[])
    view = wait_for(run_id, settled)
    assert view["status"] == pv.S_GATE
    state = json.loads((prun.autonomy_dir(run_id) / "autonomy_state.json").read_text())
    chain = [b["profile_id"] for b in state["roles"]["implementer_chain"]]
    assert chain[:6] == ["GPT6_LUNA_HIGH", "GPT6_LUNA_VERY_HIGH", "GPT6_LUNA_MAX", "TERRA_HIGH", "TERRA_VERY_HIGH",
                         "TERRA_MAX"]
    assert next(e for e in state["executions"] if e["executor"] == "execute")["profile"] == "GPT6_LUNA_HIGH"


def test_invalid_user_chain_blocks_start_and_setup_endpoint_reports_the_chain(env):
    env.install("codex")
    detection = prun.detection_snapshot(refresh=True)
    bad = prun.preview_task(form(env, implementer_chain=["TERRA_HIGH", "NOPE"]), detection=detection)
    assert not bad["can_start"] and any("nieznane profile" in b for b in bad["blockers"])
    with pytest.raises(prun.ProductError):
        prun.start_task(form(env, implementer_chain=["TERRA_HIGH", "NOPE"]), detection=detection)
    with pytest.raises(prun.ProductError):
        prun.normalize_form(form(env, implementer_chain="TERRA_HIGH"))
    setup = prun.resolve_setup({}, detection=detection, implementer_chain=["TERRA_MAX", "TERRA_HIGH"])
    assert setup["implementer_chain"]["source"] == "USER"
    assert setup["groups"]["implementation"]["models"] == [
        s["display"] for s in setup["implementer_chain"]["steps"]]
    default = prun.resolve_setup({}, detection=detection)
    assert default["implementer_chain"]["source"] == "DEFAULT"
    assert default["implementer_chain"]["default_profile_ids"][0] == "GPT6_LUNA_HIGH"


def test_verifying_sonnet_55_adds_its_steps_to_the_default_chain(env):
    env.install("claude", "codex")
    detection = prun.detection_snapshot(refresh=True)
    before = prun.resolve_setup({}, detection=detection)
    assert [s["profile_id"] for s in before["implementer_chain"]["skipped"]] == [
        "CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH"]
    assert {"CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH"} <= set(before["groups"]["implementation"]["checkable"])
    prun.verify_models(before["groups"]["implementation"]["checkable"])
    after = prun.resolve_setup({}, detection=prun.detection_snapshot())
    assert [s["profile_id"] for s in after["implementer_chain"]["steps"]][-2:] == [
        "CLAUDE_SONNET_5_5_MEDIUM", "CLAUDE_SONNET_5_5_HIGH"]
    assert not after["implementer_chain"]["skipped"]
