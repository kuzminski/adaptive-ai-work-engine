#!/usr/bin/env python3
"""Dependency-free model catalog access and binding validation for AAW V0.3."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from aaw_paths import MODEL_REGISTRY_PATH


ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "MODEL_CATALOG.json"
PROFILES_PATH = ROOT / "IMPLEMENTER_PROFILES.json"
MODEL_REGISTRY_FOR_SELFTEST = MODEL_REGISTRY_PATH
PAID_ACCESS_CLASSES = {"VERIFIED_CREDIT_REQUIRED", "VERIFIED_API_ONLY"}
LOCAL_ACCESS_CLASSES = {"LOCAL_INCLUDED"}
# Roles that must never be auto-bound to a local model in V0.1. Local presets are
# human-selectable only and are kept out of MODEL_REGISTRY.active_bindings.
LOCAL_FORBIDDEN_AUTOMATIC_ROLES = {"CODE_IMPLEMENTER", "ARCHITECT_STRONG", "INDEPENDENT_REVIEWER", "RESEARCH_SYNTHESIZER"}


class CatalogError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"{path.name} must contain a JSON object")
    return value


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict[str, Any]]:
    data = _read(path)
    rows = data.get("models")
    if not isinstance(rows, list):
        raise CatalogError("MODEL_CATALOG.models must be an array")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("runtime_model_id"), str):
            raise CatalogError("every catalog row needs runtime_model_id")
        key = str(row["runtime_model_id"])
        if key in result:
            raise CatalogError(f"duplicate model: {key}")
        result[key] = dict(row)
    return result


def load_profiles(path: Path = PROFILES_PATH) -> dict[str, dict[str, Any]]:
    data = _read(path)
    rows = data.get("profiles")
    if not isinstance(rows, list):
        raise CatalogError("IMPLEMENTER_PROFILES.profiles must be an array")
    return {str(row["profile_id"]): dict(row) for row in rows if isinstance(row, Mapping) and row.get("profile_id")}


def validate_model_effort(model_id: str, effort: str, catalog: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    models = dict(catalog or load_catalog())
    model = models.get(model_id)
    if not model:
        raise CatalogError(f"model is not in current catalog: {model_id}")
    efforts = model.get("supported_efforts")
    if not isinstance(efforts, list) or effort not in efforts:
        raise CatalogError(f"unsupported model/effort combination: {model_id}/{effort}")
    return dict(model)


def is_local_model(model: Mapping[str, Any]) -> bool:
    return str(model.get("provider")).upper() == "LOCAL" or model.get("access_class") in LOCAL_ACCESS_CLASSES


def local_profile_ids() -> list[str]:
    """Curated local presets. Human-selectable only; never auto-bound in V0.1."""
    return [pid for pid, row in load_profiles().items() if row.get("local_runtime") is True]


def resolve_profile(profile_id: str, *, allow_paid: bool = False) -> dict[str, Any]:
    profiles = load_profiles()
    profile = profiles.get(profile_id)
    if not profile:
        raise CatalogError(f"unknown execution profile: {profile_id}")
    model = validate_model_effort(str(profile.get("runtime_model_id") or ""), str(profile.get("effort") or ""))
    local = is_local_model(model)
    if not model.get("account_available"):
        raise CatalogError(f"profile is unavailable on current runtime/account: {profile_id}")
    if not local and not model.get("runtime_available"):
        raise CatalogError(f"profile is unavailable on current runtime/account: {profile_id}")
    if model.get("access_class") in PAID_ACCESS_CLASSES and not allow_paid:
        raise CatalogError(f"profile requires paid credits and NO_EXTRA_PAID_USAGE is active: {profile_id}")
    return {
        "profile_id": profile_id,
        "harness": model["harness"],
        "provider": model["provider"],
        "runtime_model_id": model["runtime_model_id"],
        "effort": profile["effort"],
        "access_class": model["access_class"],
        "automatic_use_allowed": bool(model.get("automatic_use_allowed")),
        "binding_source": "HUMAN_OVERRIDE",
        "role_hint": model.get("role_hint"),
        "local_runtime": local,
        "runtime_available_policy": model.get("runtime_available_policy"),
        "endpoint": model.get("endpoint"),
        "external_paid_cost": model.get("external_paid_cost", 0) if local else None,
        "runtime_preflight_required": local,
        "not_implementer": bool(profile.get("not_implementer")) or local,
    }


def self_test() -> int:
    models = load_catalog()
    profiles = load_profiles()
    assert "gpt-6-astra" in models and "ASTRA_XHIGH" in profiles
    assert validate_model_effort("gpt-6-astra", "xhigh")["account_available"] is True
    try:
        validate_model_effort("gpt-6-astra", "none")
    except CatalogError:
        pass
    else:
        raise AssertionError("Astra/none must be rejected")
    try:
        resolve_profile("FABLE_HIGH")
    except CatalogError as exc:
        assert "unavailable" in str(exc) or "credits" in str(exc)
    else:
        raise AssertionError("Fable must be blocked by default")

    # LOCAL Qwen: present, non-paid, resolvable by explicit human selection,
    # kept out of automatic role bindings.
    assert "qwen3-vl:4b-instruct" in models
    local_model = models["qwen3-vl:4b-instruct"]
    assert is_local_model(local_model) and local_model["access_class"] not in PAID_ACCESS_CLASSES
    assert local_model["harness"] == "ollama_openai_compat"
    assert set(local_profile_ids()) == {"LOCAL_QWEN_FAST", "LOCAL_QWEN_JSON", "LOCAL_QWEN_DELTA", "LOCAL_QWEN_SUMMARY", "LOCAL_QWEN_LOG_TRIAGE", "LOCAL_QWEN_DIFF_TRIAGE", "LOCAL_QWEN_FINDINGS_PREP"}
    for pid in local_profile_ids():
        resolved = resolve_profile(pid)  # must not raise even though runtime is dynamic
        assert resolved["local_runtime"] is True
        assert resolved["not_implementer"] is True
        assert resolved["external_paid_cost"] == 0
        assert resolved["runtime_preflight_required"] is True
    registry_path = MODEL_REGISTRY_FOR_SELFTEST
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        bound_models = {b.get("runtime_model_id") for b in registry.get("active_bindings", [])}
        assert "qwen3-vl:4b-instruct" not in bound_models, "local model must not be an automatic binding"

    print(json.dumps({"status": "PASS", "models": len(models), "profiles": len(profiles), "local_profiles": len(local_profile_ids())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
