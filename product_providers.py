#!/usr/bin/env python3
"""AAW PRODUCT MVP V0.1 — local provider (AI CLI) detection.

AAW drives locally installed AI CLIs; it never holds a password, API key or
token of its own. This module answers, for each supported CLI:

  * FOUND / NOT_FOUND (and where),
  * the CLI version (`<cli> --version`),
  * the login status, *only* through the CLI's own read-only status command
    (`claude auth status`, `codex login status`). Credential files are never
    opened. Anything that cannot be determined safely is reported UNKNOWN,
  * which AAW profiles (IMPLEMENTER_PROFILES) bind to that CLI and whether
    each is runnable now (`workflow_runner.profile_availability`, the same
    check the autonomy adapters use before every call).

Architecture for later providers: a provider is one `ProviderSpec` entry. The
harness name links it to IMPLEMENTER_PROFILES / MODEL_CATALOG; adding a CLI
is a new spec plus profiles, no change to the detection code.
"""

from __future__ import annotations

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
        setup_help={"install": "Zainstaluj Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code",
                    "login": "W terminalu uruchom: claude  (lub: claude auth login) i zaloguj się swoim kontem.",
                    "verify": "claude auth status"}),
    ProviderSpec(
        provider_id="codex", display_name="Codex CLI", harness="codex",
        version_args=("--version",), login_args=("login", "status"), login_parser=_codex_login,
        setup_help={"install": "Zainstaluj Codex CLI: https://github.com/openai/codex",
                    "login": "W terminalu uruchom: codex login  i zaloguj się swoim kontem.",
                    "verify": "codex login status"}),
]


def register_provider(spec: ProviderSpec) -> None:
    """Extension point for later providers (replaces a spec with the same id)."""
    PROVIDERS[:] = [p for p in PROVIDERS if p.provider_id != spec.provider_id] + [spec]


def _profiles_for(harness: str) -> list[dict[str, Any]]:
    try:
        profiles = wr.load_implementer_profiles()
    except wr.WorkflowStop as exc:
        return [{"profile_id": None, "runnable": False, "reason": str(exc)}]
    rows = []
    for profile_id, profile in profiles.items():
        if profile.get("harness") != harness:
            continue
        status, reason = wr.profile_availability(profile)
        rows.append({"profile_id": profile_id, "runtime_model_id": profile.get("runtime_model_id"),
                     "effort": profile.get("effort"), "static_availability": profile.get("availability"),
                     "runnable": status == "VERIFIED", "reason": reason})
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
    import datetime as dt
    providers = [detect_provider(spec, runner=runner, which=which, timeout=timeout) for spec in PROVIDERS]
    return {"detected_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "notice": PROVIDER_NOTICE, "providers": providers,
            "git": detect_git(runner=runner, which=which),
            "any_found": any(p["status"] == FOUND for p in providers)}


def usable_harnesses(detection: Mapping[str, Any]) -> set[str]:
    """Harnesses whose CLI is installed and not known to be logged out."""
    return {p["harness"] for p in detection.get("providers", [])
            if p.get("status") == FOUND and p.get("login") != NOT_LOGGED_IN}


def runnable_profiles(detection: Mapping[str, Any]) -> set[str]:
    usable = usable_harnesses(detection)
    return {pid for p in detection.get("providers", []) if p["harness"] in usable
            for pid in p.get("runnable_profiles", [])}


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
