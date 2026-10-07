#!/usr/bin/env python3
"""AAW PROVIDER ADAPTER CONTRACT V0.1 — what a provider must offer to take part in routing.

`model_router` decides *which* profile should serve a call; this module defines
what a non-built-in provider (anything besides the direct Claude / Codex CLI
path in `autonomy_adapters`) has to implement so that decision can be honoured:

    detect()          is the CLI/service present, and what does it claim to support?
    quota_snapshot()  honest telemetry for model_router (EXACT / ESTIMATED / UNKNOWN)
    preflight()       None if a profile is runnable now, else the reason
    invoke()          run one role call and return the structured result

Google models are served today through the Antigravity CLI (`agy`), a built-in
direct CLI harness in `autonomy_adapters` with detection and model probes
in `product_providers` (see AAW_IMPLEMENTER_EFFECTIVENESS_PLAN_V0_1.md).

Google Antigravity (FREE) is the first adapter-contract provider. Its trust level is
EXPERIMENTAL. Live integration is NOT_TESTED here: no Antigravity CLI is
installed in the development environment, so `AntigravityAdapter` implements
capability detection and the contract, and refuses to dispatch (a clean,
non-dispatched UNAVAILABLE failure the router fails over from). It never guesses
CLI flags. Wiring a verified CLI means implementing `_spawn` and flipping the
profile to VERIFIED — no routing change.

`ScriptedProviderAdapter` is the contract's test double.
"""

from __future__ import annotations

import shutil
from typing import Any, Callable, Mapping, Sequence

CONTRACT_VERSION = "AAW_PROVIDER_ADAPTER_V0.1"

LIVE_NOT_TESTED = "NOT_TESTED"
LIVE_VERIFIED = "VERIFIED"
LIVE_UNAVAILABLE = "UNAVAILABLE"

# The capability vocabulary model_router's `required_capabilities` draws from.
CAPABILITIES = ("code_edit", "shell", "structured_output", "code_read", "review", "planning")


class ProviderAdapter:
    """Contract. Subclasses override; defaults are the safe, honest answers."""

    provider_id = "unknown"
    pool = "unknown"
    harness = "unknown"
    trust = "EXPERIMENTAL"
    declared_capabilities: tuple[str, ...] = ()

    def detect(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "contract": CONTRACT_VERSION, "status": LIVE_UNAVAILABLE,
                "live_integration": LIVE_NOT_TESTED, "trust": self.trust, "executable": None,
                "declared_capabilities": list(self.declared_capabilities), "verified_capabilities": [],
                "reason": "adapter does not implement detection"}

    def quota_snapshot(self) -> dict[str, Any]:
        """Pool telemetry for model_router. The honest default is UNKNOWN: no percent is ever invented."""
        return {"limits": [{"window": "unknown", "certainty": "UNKNOWN", "remaining_percent": None,
                            "source": f"{self.provider_id}:no-telemetry"}]}

    def preflight(self, profile: Mapping[str, Any]) -> str | None:
        detected = self.detect()
        if detected["status"] != "FOUND":
            return detected["reason"]
        if detected["live_integration"] != LIVE_VERIFIED:
            return f"{self.provider_id} live integration is {detected['live_integration']}"
        return None

    def invoke(self, ctx: Mapping[str, Any], runtime: Mapping[str, Any], handoff: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class AntigravityAdapter(ProviderAdapter):
    provider_id = "google-antigravity"
    pool = "google-antigravity"
    harness = "antigravity"
    trust = "EXPERIMENTAL"
    # What the router is told the free tier can do; none of it is verified live.
    declared_capabilities = ("code_edit", "shell", "structured_output", "code_read")
    EXECUTABLE_NAMES = ("antigravity", "agy")

    def __init__(self, *, which: Callable[[str], str | None] | None = None) -> None:
        self._which = which or shutil.which

    def detect(self) -> dict[str, Any]:
        found = next((path for path in (self._which(n) for n in self.EXECUTABLE_NAMES) if path), None)
        base = {"provider_id": self.provider_id, "contract": CONTRACT_VERSION, "trust": self.trust,
                "declared_capabilities": list(self.declared_capabilities), "verified_capabilities": [],
                "live_integration": LIVE_NOT_TESTED, "executable": found}
        if not found:
            return {**base, "status": LIVE_UNAVAILABLE,
                    "reason": "Antigravity CLI not found on PATH; live integration NOT_TESTED"}
        return {**base, "status": "FOUND",
                "reason": "CLI present but no live dispatch has been verified (NOT_TESTED); the profile stays unavailable"}

    def invoke(self, ctx: Mapping[str, Any], runtime: Mapping[str, Any], handoff: Mapping[str, Any]) -> dict[str, Any]:
        from autonomy_controller import ExecutorFailure
        # Nothing is spawned: the failure is pre-dispatch, so the router can safely fail over.
        raise ExecutorFailure("Antigravity live dispatch is NOT_TESTED / not implemented", dispatched=False,
                              failure_class="UNAVAILABLE")


class ScriptedProviderAdapter(ProviderAdapter):
    """Contract test double: replays a script of outcomes ('ok' dicts or exceptions)."""

    def __init__(self, provider_id: str = "scripted", *, trust: str = "EXPERIMENTAL",
                 capabilities: Sequence[str] = ("code_edit", "shell", "structured_output"),
                 outcomes: Sequence[Any] = (), telemetry: Mapping[str, Any] | None = None,
                 executable: str | None = "/usr/bin/scripted") -> None:
        self.provider_id = self.pool = self.harness = provider_id
        self.trust, self.declared_capabilities = trust, tuple(capabilities)
        self._outcomes, self._telemetry, self._executable = list(outcomes), telemetry, executable
        self.calls: list[Mapping[str, Any]] = []

    def detect(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "contract": CONTRACT_VERSION, "trust": self.trust,
                "status": "FOUND" if self._executable else LIVE_UNAVAILABLE, "executable": self._executable,
                "live_integration": LIVE_VERIFIED if self._executable else LIVE_UNAVAILABLE,
                "declared_capabilities": list(self.declared_capabilities),
                "verified_capabilities": list(self.declared_capabilities) if self._executable else [],
                "reason": None if self._executable else "scripted adapter has no executable"}

    def quota_snapshot(self) -> dict[str, Any]:
        return dict(self._telemetry) if self._telemetry else super().quota_snapshot()

    def invoke(self, ctx: Mapping[str, Any], runtime: Mapping[str, Any], handoff: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(runtime))
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else (self._outcomes[0] if self._outcomes else {})
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)


# ── registry (the direct-CLI executor consults it for harnesses it does not know) ──

PROVIDER_ADAPTERS: dict[str, ProviderAdapter] = {}


def register_provider_adapter(adapter: ProviderAdapter) -> None:
    PROVIDER_ADAPTERS[adapter.harness] = adapter


def unregister_provider_adapter(harness: str) -> None:
    PROVIDER_ADAPTERS.pop(harness, None)


def collect_telemetry() -> dict[str, Any]:
    """Merge every registered adapter's snapshot into `model_router` telemetry shape."""
    return {"pools": {a.pool: a.quota_snapshot() for a in PROVIDER_ADAPTERS.values()}}


def status_report() -> dict[str, Any]:
    """Provider capability detection for diagnostics / evidence (`live_integration` is explicit)."""
    return {"contract": CONTRACT_VERSION,
            "providers": {h: a.detect() for h, a in PROVIDER_ADAPTERS.items()}}


register_provider_adapter(AntigravityAdapter())
