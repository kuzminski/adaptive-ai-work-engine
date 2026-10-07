#!/usr/bin/env python3
"""AAW PRODUCT MVP V0.2 — local provider (AI CLI) detection and model availability.

AAW drives locally installed AI CLIs; it never holds a password, API key or
token of its own. This module answers, for each supported CLI:

  * FOUND / NOT_FOUND (and where),
  * the CLI version (`<cli> --version`),
  * the login status, *only* through the CLI's own read-only status command
    (`claude auth status`, `codex login status`, and for Antigravity CLI the
    print-mode `/usage` answer, which starts no agent turn and spends no
    quota). Credential files are never opened. Anything that cannot be determined safely is reported UNKNOWN,
  * which AAW profiles (IMPLEMENTER_PROFILES) bind to that CLI and whether
    each is runnable now (`workflow_runner.profile_availability`, the same
    check the autonomy adapters use before every call).

Model availability shown to the user is the combination of two sources and
never a guess (V0.2):

    versioned AAW policy/catalog (IMPLEMENTER_PROFILES + MODEL_CATALOG)
  + actual local runtime capability (the installed CLI, its login, and — for
    models marked DYNAMIC_PREFLIGHT / LOCAL_RUNTIME_PROBE_REQUIRED — an
    explicit, user-started probe that sends one tiny request per model)
  → one availability state per profile.

A profile that only exists as an *exact runtime mapping* of a frozen V0.3
tier (e.g. CLAUDE_OPUS_5_5_HIGH for OPUS_5_5_HIGH) is usable only after the
probe saw the installed CLI accept that exact model ID. A rejected probe makes
a model unavailable; an offline/unknown probe changes nothing. Nothing is ever
substituted silently — resolution shows every alternative before START.

Architecture for later providers: a provider is one `ProviderSpec` entry. The
harness name links it to IMPLEMENTER_PROFILES / MODEL_CATALOG; adding a CLI
is a new spec plus profiles, no change to the detection code.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import workflow_runner as wr

LOGGED_IN = "LOGGED_IN"
NOT_LOGGED_IN = "NOT_LOGGED_IN"
LOGIN_UNKNOWN = "UNKNOWN"
FOUND = "FOUND"
NOT_FOUND = "NOT_FOUND"

PROVIDER_NOTICE = ("AAW korzysta z lokalnie zainstalowanych CLI. Provider wymaga własnego aktywnego "
                   "konta/loginu. AAW nie przechowuje haseł ani kluczy.")
SETUP_NOTICE = ("AAW potrzebuje co najmniej jednego obsługiwanego CLI AI (Claude CLI, Codex CLI lub Antigravity CLI), "
                "zainstalowanego i zalogowanego na Twoim koncie. Instalacja i logowanie odbywają się poza AAW — "
                "AAW tylko wykrywa CLI i z niego korzysta.")

# Per-profile availability states (user-visible).
A_AVAILABLE = "AVAILABLE"              # policy allows, CLI found and not logged out
A_NOT_VERIFIED = "NOT_VERIFIED"        # policy allows, runtime check pending (DYNAMIC_PREFLIGHT); usable, warned
A_VERIFIED_HERE = "VERIFIED_HERE"      # the local probe saw the CLI accept this exact model
A_NEEDS_CHECK = "NEEDS_CHECK"          # exact mapping that requires a positive local probe before use
A_REJECTED_HERE = "REJECTED_HERE"      # the local probe saw the CLI reject this model ID
A_POLICY = "POLICY_UNAVAILABLE"        # the versioned catalog marks it unavailable (e.g. unmapped, paid-only)
A_CLI_MISSING = "CLI_NOT_FOUND"
A_LOGGED_OUT = "NOT_LOGGED_IN"
RUNNABLE_STATES = {A_AVAILABLE, A_NOT_VERIFIED, A_VERIFIED_HERE}
AVAILABILITY_LABEL = {
    A_AVAILABLE: "dostępny", A_NOT_VERIFIED: "dostępny wg katalogu, jeszcze nie sprawdzony tutaj",
    A_VERIFIED_HERE: "sprawdzony na tym komputerze", A_NEEDS_CHECK: "wymaga sprawdzenia przed użyciem",
    A_REJECTED_HERE: "odrzucony przez CLI na tym komputerze", A_POLICY: "niedostępny wg katalogu AAW",
    A_CLI_MISSING: "brak CLI", A_LOGGED_OUT: "CLI niezalogowane",
}
PROBE_REQUIRED = "LOCAL_RUNTIME_PROBE_REQUIRED"
PROBE_ACCEPTED, PROBE_REJECTED, PROBE_UNKNOWN = "ACCEPTED", "REJECTED", "UNKNOWN"
PROBE_TOKEN = "AAW_MODEL_OK"
PROBES_FILE = "model_probes.json"

Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


def _default_runner(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    env = dict(os.environ)
    # A parent Claude Code session id must never leak into a probe either.
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID"):
        env.pop(name, None)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL, **kwargs)
    except FileNotFoundError:
        return 127, "", "executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 126, "", str(exc)
    return done.returncode, done.stdout or "", done.stderr or ""


def _parse_version(text: str) -> str | None:
    match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+.][0-9A-Za-z.]+)?)", text or "")
    return match.group(1) if match else None


def _claude_login(rc: int, out: str, err: str) -> tuple[str, str]:
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("loggedIn"), bool):
        method = data.get("authMethod")
        return (LOGGED_IN, f"zalogowano ({method})" if method else "zalogowano") if data["loggedIn"] \
            else (NOT_LOGGED_IN, "brak aktywnego logowania")
    text = f"{out}\n{err}".lower()
    if rc != 0 and ("not logged in" in text or "login" in text and "required" in text):
        return NOT_LOGGED_IN, "brak aktywnego logowania"
    return LOGIN_UNKNOWN, "nie można bezpiecznie ustalić statusu logowania"


def _codex_login(rc: int, out: str, err: str) -> tuple[str, str]:
    text = f"{out}\n{err}".lower()
    if "not logged in" in text:
        return NOT_LOGGED_IN, "brak aktywnego logowania"
    if rc == 0 and "logged in" in text:
        return LOGGED_IN, (out or err).strip().splitlines()[0][:120]
    if rc not in (0, 124, 126, 127) and "unknown" not in text and "unrecognized" not in text:
        return NOT_LOGGED_IN, "CLI zgłosiło brak logowania"
    return LOGIN_UNKNOWN, "nie można bezpiecznie ustalić statusu logowania"


def _agy_login(rc: int, out: str, err: str) -> tuple[str, str]:
    """`agy -p /usage --output-format json`: answered locally when signed in; a sign-in error otherwise."""
    text = f"{out}\n{err}".lower()
    if rc == 0 and out.strip():
        return LOGGED_IN, "zalogowano (Antigravity)"
    if any(p in text for p in ("sign in", "sign-in", "signed in", "/login", "not logged in", "authenticat")):
        return NOT_LOGGED_IN, "brak aktywnego logowania Antigravity"
    return LOGIN_UNKNOWN, "nie można bezpiecznie ustalić statusu logowania"


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    harness: str                        # links to IMPLEMENTER_PROFILES.harness
    version_args: tuple[str, ...]
    login_args: tuple[str, ...] | None
    login_parser: Callable[[int, str, str], tuple[str, str]] | None
    setup_help: Mapping[str, str] = field(default_factory=dict)


PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(
        provider_id="claude", display_name="Claude CLI", harness="claude",
        version_args=("--version",), login_args=("auth", "status"), login_parser=_claude_login,
        setup_help={"install": "Zainstaluj Claude Code (Claude CLI) według instrukcji: "
                               "https://docs.anthropic.com/en/docs/claude-code/setup "
                               "(np. z Node.js: npm install -g @anthropic-ai/claude-code).",
                    "login": "Zaloguj się raz: otwórz terminal, wpisz  claude  i postępuj zgodnie z instrukcjami "
                             "(lub: claude auth login). Potem wróć tutaj i kliknij „Wykryj ponownie”.",
                    "verify": "claude auth status",
                    "docs": "https://docs.anthropic.com/en/docs/claude-code/setup"}),
    ProviderSpec(
        provider_id="codex", display_name="Codex CLI", harness="codex",
        version_args=("--version",), login_args=("login", "status"), login_parser=_codex_login,
        setup_help={"install": "Zainstaluj Codex CLI według instrukcji: https://github.com/openai/codex "
                               "(np. z Node.js: npm install -g @openai/codex).",
                    "login": "Zaloguj się raz: otwórz terminal, wpisz  codex login  i postępuj zgodnie z instrukcjami. "
                             "Potem wróć tutaj i kliknij „Wykryj ponownie”.",
                    "verify": "codex login status",
                    "docs": "https://github.com/openai/codex"}),
    ProviderSpec(
        provider_id="antigravity", display_name="Antigravity CLI", harness="agy",
        version_args=("--version",), login_args=("-p", "/usage", "--output-format", "json"),
        login_parser=_agy_login,
        setup_help={"install": "Zainstaluj Antigravity CLI (agy): w PowerShell  irm https://antigravity.google/cli/install.ps1 | iex  "
                               "(macOS/Linux: curl -fsSL https://antigravity.google/cli/install.sh | bash).",
                    "login": "Zaloguj się raz: otwórz terminal, wpisz  agy  i zaloguj się kontem Google. Potem wróć "
                             "tutaj, kliknij „Wykryj ponownie” i „Sprawdź modele”.",
                    "verify": "agy --version",
                    "docs": "https://antigravity.google/docs/cli/overview"}),
]


def register_provider(spec: ProviderSpec) -> None:
    """Extension point for later providers (replaces a spec with the same id)."""
    PROVIDERS[:] = [p for p in PROVIDERS if p.provider_id != spec.provider_id] + [spec]


def _catalog_models() -> dict[str, dict[str, Any]]:
    try:
        from model_catalog import load_catalog
        return load_catalog()
    except Exception:
        return {}


def _profiles_for(harness: str) -> list[dict[str, Any]]:
    """Static (policy/catalog + executable) facts per profile; the final state is `profile_states`."""
    try:
        profiles = wr.load_implementer_profiles()
    except wr.WorkflowStop as exc:
        return [{"profile_id": None, "runnable": False, "reason": str(exc)}]
    models = _catalog_models()
    rows = []
    for profile_id, profile in profiles.items():
        if profile.get("harness") != harness:
            continue
        status, reason = wr.profile_availability(profile)
        model = models.get(str(profile.get("runtime_model_id") or "")) or {}
        rows.append({"profile_id": profile_id, "display_name": profile.get("display_name") or profile_id,
                     "runtime_model_id": profile.get("runtime_model_id"),
                     "model_family": model.get("model_family"),
                     "effort": profile.get("effort"), "static_availability": profile.get("availability"),
                     "static_runnable": status == "VERIFIED", "runnable": status == "VERIFIED", "reason": reason,
                     "probe_required": profile.get("availability_policy") == PROBE_REQUIRED,
                     "dynamic_preflight": model.get("runtime_available_policy") == "DYNAMIC_PREFLIGHT",
                     "exact_runtime_mapping_of": profile.get("exact_runtime_mapping_of")})
    return rows


def detect_provider(spec: ProviderSpec, *, runner: Runner | None = None,
                    which: Callable[[str], str | None] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    runner = runner or _default_runner
    executable = (which or wr.harness_executable)(spec.harness)
    row: dict[str, Any] = {"provider_id": spec.provider_id, "display_name": spec.display_name,
                           "harness": spec.harness, "status": NOT_FOUND, "executable": None, "version": None,
                           "login": LOGIN_UNKNOWN, "login_detail": None, "profiles": [],
                           "runnable_profiles": [], "setup_help": dict(spec.setup_help)}
    if not executable:
        row["login_detail"] = "CLI nie jest zainstalowane lub nie ma go w PATH"
        row["profiles"] = _profiles_for(spec.harness)
        return row
    row.update(status=FOUND, executable=str(executable))
    rc, out, err = runner([str(executable), *spec.version_args], timeout)
    row["version"] = _parse_version(out or err) if rc == 0 else None
    row["version_raw"] = (out or err).strip()[:200]
    if spec.login_args and spec.login_parser:
        rc, out, err = runner([str(executable), *spec.login_args], timeout)
        row["login"], row["login_detail"] = spec.login_parser(rc, out, err)
    row["profiles"] = _profiles_for(spec.harness)
    row["runnable_profiles"] = [p["profile_id"] for p in row["profiles"] if p.get("runnable")]
    return row


# ── model probes (explicit user action; one tiny request per model) ─────────

def _probe_key(harness: str, model: str) -> str:
    return f"{harness}|{model}"


def load_probes() -> dict[str, dict[str, Any]]:
    import product_home
    data = product_home.read_json(product_home.home() / PROBES_FILE, {}) or {}
    probes = data.get("probes") if isinstance(data, dict) else None
    return probes if isinstance(probes, dict) else {}


def _save_probes(probes: Mapping[str, Any]) -> None:
    import product_home
    product_home.write_json(product_home.home() / PROBES_FILE,
                            {"schema_version": "AAW_MODEL_PROBES_V1", "probes": dict(probes),
                             "note": "Local runtime evidence only: which exact model IDs the installed CLI "
                                     "accepted or rejected. Keyed by CLI and model; ignored after a CLI version "
                                     "change."})


def probe_argv(harness: str, executable: str, model: str, *, minimal: bool = True) -> list[str]:
    prompt = f"Reply with exactly: {PROBE_TOKEN}"
    if harness == "codex":
        return [executable, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--model", model, "--config", 'model_reasoning_effort="low"', prompt]
    if harness == "agy":
        # Default permission mode: no tool can be approved in print mode, so the probe stays a plain answer.
        return [executable, "--model", model, "--output-format", "json", "--print", prompt]
    argv = [executable, "--print", "--no-session-persistence", "--model", model, "--effort", "low",
            "--output-format", "json", "--max-turns", "1", "--permission-mode", "plan"]
    if minimal:  # no tools and a one-line system prompt keep the probe to a few hundred tokens
        argv += ["--tools", "", "--system-prompt", "You are a connectivity probe. Follow the instruction exactly."]
    return argv + [prompt]


_MODEL_REJECTED = re.compile(
    r"unrecognized_model|issue with the selected model|model[^\n]{0,120}(does not exist|not exist|not found|"
    r"no access|not have access|not available|unsupported|not supported|invalid|unknown)|"
    r"(unknown|invalid|unsupported) model", re.IGNORECASE)
_TRANSIENT = re.compile(r"overloaded|rate.?limit|\b429\b|\b5\d\d\b|timed? ?out|connection (error|refused|reset)|"
                        r"network|ENOTFOUND|ECONNRESET|getaddrinfo|offline", re.IGNORECASE)
_LOGGED_OUT = re.compile(r"not logged in|please log ?in|login required|unauthori[sz]ed|\b401\b|invalid api key|"
                         r"not signed in|sign in to|run /login", re.IGNORECASE)


def classify_probe(harness: str, rc: int, out: str, err: str) -> tuple[str, str]:
    """(ACCEPTED | REJECTED | UNKNOWN, plain-language detail). Never guesses ACCEPTED."""
    text = f"{out}\n{err}"
    data = None
    if harness == "claude":
        try:
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
        except (json.JSONDecodeError, IndexError):
            data = None
    if harness == "agy":
        try:
            data = json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            data = None
        if rc == 0 and isinstance(data, dict) and PROBE_TOKEN in str(data.get("response") or ""):
            return PROBE_ACCEPTED, "CLI przyjęło dokładny identyfikator modelu i model odpowiedział"
        data = None
    if rc == 0 and PROBE_TOKEN in (out or ""):
        if not isinstance(data, dict) or (data.get("is_error") is False):
            return PROBE_ACCEPTED, "CLI przyjęło dokładny identyfikator modelu i model odpowiedział"
    if isinstance(data, dict) and data.get("api_error_status") == 404:
        return PROBE_REJECTED, str(data.get("result") or "CLI zgłosiło: model nieznany lub brak dostępu")[:240]
    if rc in (124,):
        return PROBE_UNKNOWN, "sprawdzenie przekroczyło limit czasu (brak sieci?) — bez zmian"
    if rc in (126, 127):
        return PROBE_UNKNOWN, "nie udało się uruchomić CLI"
    if _LOGGED_OUT.search(text):
        return PROBE_UNKNOWN, "CLI nie jest zalogowane — zaloguj się i sprawdź ponownie"
    if _TRANSIENT.search(text):
        return PROBE_UNKNOWN, "chwilowy problem (sieć, przeciążenie lub limit) — bez zmian; spróbuj później"
    if _MODEL_REJECTED.search(text):
        snippet = next((line.strip() for line in text.splitlines() if _MODEL_REJECTED.search(line)), "")
        return PROBE_REJECTED, ("CLI odrzuciło ten model: " + snippet)[:240]
    snippet = " ".join(text.split())[:200]
    return PROBE_UNKNOWN, f"nie udało się ustalić (sieć/offline lub inny błąd CLI): {snippet}" if snippet else \
        "nie udało się ustalić (sieć/offline lub inny błąd CLI)"


def probe_model(harness: str, executable: str, model: str, *, runner: Runner | None = None,
                timeout: float = 90.0) -> dict[str, Any]:
    runner = runner or _default_runner
    rc, out, err = runner(probe_argv(harness, executable, model), timeout)
    if harness == "claude" and rc != 0 and re.search(r"unknown option|unrecognized option|error: option",
                                                     f"{out}\n{err}", re.IGNORECASE):
        rc, out, err = runner(probe_argv(harness, executable, model, minimal=False), timeout)  # older CLI
    status, detail = classify_probe(harness, rc, out, err)
    return {"harness": harness, "model": model, "status": status, "detail": detail,
            "probed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}


def _probe_for(probes: Mapping[str, Any], harness: str, model: str | None, version: str | None) -> dict | None:
    if not model:
        return None
    row = probes.get(_probe_key(harness, model))
    if not isinstance(row, dict):
        return None
    if version and row.get("cli_version") and row["cli_version"] != version:
        return None  # CLI changed since the probe: the evidence no longer applies
    return row


def profile_states(detection: Mapping[str, Any], probes: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Final user-visible availability per profile: catalog policy + local runtime + probe evidence."""
    probes = load_probes() if probes is None else probes
    out: dict[str, dict[str, Any]] = {}
    for provider in detection.get("providers", []):
        harness = provider.get("harness")
        for row in provider.get("profiles", []):
            pid = row.get("profile_id")
            if not pid:
                continue
            probe = _probe_for(probes, harness, row.get("runtime_model_id"), provider.get("version"))
            if provider.get("status") != FOUND:
                state, reason = A_CLI_MISSING, f"{provider.get('display_name')} nie jest zainstalowane"
            elif provider.get("login") == NOT_LOGGED_IN:
                state, reason = A_LOGGED_OUT, f"{provider.get('display_name')} nie jest zalogowane"
            elif not row.get("static_runnable", row.get("runnable")):
                state, reason = A_POLICY, row.get("reason") or "niedostępny wg katalogu AAW"
            elif probe and probe.get("status") == PROBE_REJECTED:
                state, reason = A_REJECTED_HERE, probe.get("detail")
            elif probe and probe.get("status") == PROBE_ACCEPTED:
                state, reason = A_VERIFIED_HERE, f"sprawdzono {probe.get('probed_at')}"
            elif row.get("probe_required"):
                state, reason = A_NEEDS_CHECK, ("dokładne mapowanie modelu " + str(row.get("runtime_model_id")) +
                                                " — użyte dopiero, gdy sprawdzenie na tym komputerze potwierdzi, "
                                                "że CLI akceptuje ten model")
            elif row.get("dynamic_preflight"):
                state, reason = A_NOT_VERIFIED, "katalog: dostępny; jeszcze nie sprawdzony na tym komputerze"
            else:
                state, reason = A_AVAILABLE, None
            out[pid] = {"profile_id": pid, "harness": harness, "runtime_model_id": row.get("runtime_model_id"),
                        "model_family": row.get("model_family"), "effort": row.get("effort"),
                        "display_name": row.get("display_name") or pid, "state": state,
                        "label": AVAILABILITY_LABEL[state], "reason": reason, "runnable": state in RUNNABLE_STATES,
                        "probe": probe, "exact_runtime_mapping_of": row.get("exact_runtime_mapping_of"),
                        "checkable": state in (A_NEEDS_CHECK, A_NOT_VERIFIED, A_VERIFIED_HERE, A_REJECTED_HERE)
                        and bool(row.get("runtime_model_id"))}
    return out


def apply_states(detection: dict[str, Any], probes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Refresh the per-profile `availability`/`runnable` fields and the per-provider model summary in place."""
    states = profile_states(detection, probes)
    for provider in detection.get("providers", []):
        models: dict[str, dict[str, Any]] = {}
        for row in provider.get("profiles", []):
            st = states.get(row.get("profile_id") or "")
            if not st:
                continue
            row.update(availability=st["state"], availability_label=st["label"], availability_reason=st["reason"],
                       runnable=st["runnable"])
            model = row.get("runtime_model_id")
            if model:
                entry = models.setdefault(model, {"runtime_model_id": model, "model_family": row.get("model_family"),
                                                  "states": set(), "profiles": []})
                entry["states"].add(st["state"])
                entry["profiles"].append(row["profile_id"])
        provider["runnable_profiles"] = [r["profile_id"] for r in provider.get("profiles", []) if r.get("runnable")]
        order = [A_VERIFIED_HERE, A_AVAILABLE, A_NOT_VERIFIED, A_NEEDS_CHECK, A_REJECTED_HERE, A_LOGGED_OUT,
                 A_CLI_MISSING, A_POLICY]
        provider["models"] = [{**m, "state": next(s for s in order if s in m["states"]),
                               "label": AVAILABILITY_LABEL[next(s for s in order if s in m["states"])],
                               "states": sorted(m["states"])} for m in models.values()]
    detection["usable_profile_count"] = sum(1 for s in states.values() if s["runnable"])
    return detection


def verify_models(detection: Mapping[str, Any], profile_ids: Sequence[str] | None = None, *,
                  runner: Runner | None = None, timeout: float = 90.0) -> dict[str, Any]:
    """Probe each distinct (CLI, model) behind the given profiles (default: every checkable profile)."""
    states = profile_states(detection)
    versions = {p["harness"]: p.get("version") for p in detection.get("providers", [])}
    executables = {p["harness"]: p.get("executable") for p in detection.get("providers", [])}
    wanted = [states[p] for p in (profile_ids or states) if p in states and states[p]["checkable"]]
    targets = sorted({(s["harness"], s["runtime_model_id"]) for s in wanted})
    probes = load_probes()
    results = []
    for harness, model in targets:
        if not executables.get(harness):
            continue
        result = probe_model(harness, executables[harness], model, runner=runner, timeout=timeout)
        result["cli_version"] = versions.get(harness)
        previous = probes.get(_probe_key(harness, model))
        if result["status"] == PROBE_UNKNOWN and isinstance(previous, dict):
            result["previous"] = {k: previous.get(k) for k in ("status", "probed_at", "cli_version")}
            results.append(result)
            continue  # an offline/unknown probe never overwrites earlier evidence
        probes[_probe_key(harness, model)] = result
        results.append(result)
    _save_probes(probes)
    return {"results": results, "checked": len(results)}


def detect_git(*, runner: Runner | None = None, which: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    import shutil
    executable = (which or shutil.which)("git")
    if not executable:
        return {"status": NOT_FOUND, "executable": None, "version": None,
                "help": "AAW wymaga Git (izolowany worktree). Zainstaluj Git: https://git-scm.com/downloads"}
    rc, out, err = (runner or _default_runner)([executable, "--version"], 15.0)
    return {"status": FOUND, "executable": executable, "version": _parse_version(out) if rc == 0 else None,
            "help": None}


def detect_all(*, runner: Runner | None = None, which: Callable[[str], str | None] | None = None,
               timeout: float = 20.0) -> dict[str, Any]:
    providers = [detect_provider(spec, runner=runner, which=which, timeout=timeout) for spec in PROVIDERS]
    detection = {"detected_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "notice": PROVIDER_NOTICE, "setup_notice": SETUP_NOTICE, "providers": providers,
                 "git": detect_git(runner=runner, which=which),
                 "any_found": any(p["status"] == FOUND for p in providers),
                 "any_ready": any(p["status"] == FOUND and p["login"] != NOT_LOGGED_IN for p in providers)}
    return apply_states(detection)


def usable_harnesses(detection: Mapping[str, Any]) -> set[str]:
    """Harnesses whose CLI is installed and not known to be logged out."""
    return {p["harness"] for p in detection.get("providers", [])
            if p.get("status") == FOUND and p.get("login") != NOT_LOGGED_IN}


def runnable_profiles(detection: Mapping[str, Any], probes: Mapping[str, Any] | None = None) -> set[str]:
    """Profiles a NEW run may bind now (catalog policy + local CLI + current probe evidence)."""
    return {pid for pid, st in profile_states(detection, probes).items() if st["runnable"]}


def compare_versions(found: str | None, minimum: str | None) -> bool | None:
    """True/False when both are comparable, None when unknown."""
    if not minimum:
        return True
    if not found:
        return None
    def parts(value: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", value)[:3]]
    a, b = parts(found), parts(minimum)
    if not a or not b:
        return None
    width = max(len(a), len(b))
    return a + [0] * (width - len(a)) >= b + [0] * (width - len(b))


if __name__ == "__main__":
    print(json.dumps(detect_all(), indent=2, ensure_ascii=False))
