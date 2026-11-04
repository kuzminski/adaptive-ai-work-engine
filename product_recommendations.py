#!/usr/bin/env python3
"""AAW PRODUCT MVP V0.2 — model recommendations and simple-choice resolution.

Three layers, none of which scrapes benchmarks:

  A. local detection — `product_providers` says which profiles are runnable;
  B. a built-in, versioned catalog — `MODEL_RECOMMENDATIONS.json`;
  C. an optional catalog update fetched from the AAW GitHub repository. It is
     validated, stored next to the user's data and used only when it is newer.
     No network ⇒ the built-in catalog is used; nothing blocks.

The user picks three simple levels (planning / implementation / review). This
module turns them into the V0.3 `AUTONOMY_ROLES` shape (`roles` +
`policy_profiles`) for ONE new run. The engine freezes those bindings in the
run state; a catalog update never changes a run that already started.

Resolution is visible, never silent: per policy slot the first runnable
candidate is chosen and labelled RECOMMENDED (the catalog's first choice —
for the RECOMMENDED options that is exactly the frozen V0.3 profile) or
ALTERNATIVE. A required slot with no runnable candidate blocks START; an
optional (escalation-only) slot keeps its first candidate and the engine stops
with ROLE_PROFILE_UNAVAILABLE if that escalation is ever needed.

V0.2 — exact runtime mappings: a frozen V0.3 profile with no runtime ID in the
frozen catalog (OPUS_5_5_HIGH, …) may be served by the catalog's
`exact_runtime_mappings` profile (same model and effort, exact runtime ID) —
but only when `product_providers` reports that profile runnable, i.e. a local
probe saw the installed CLI accept the exact model ID. The slot is then shown
as RECOMMENDED with `exact_mapping_of`; it is never a different model.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

import autonomy_contract as ac
import autonomy_policy as ap
import product_home
import workflow_runner as wr

ROOT = Path(__file__).resolve().parent
BUILTIN_PATH = ROOT / "MODEL_RECOMMENDATIONS.json"
DOWNLOADED_NAME = "MODEL_RECOMMENDATIONS.downloaded.json"
SCHEMA_VERSION = "AAW_MODEL_RECOMMENDATIONS_V1"
CHOICE_GROUPS = ("planning", "implementation", "review")
PROFILE_FIELDS = ("provider", "profile", "recommended_roles", "strength_class", "speed_class", "cost_class",
                  "minimum_cli_version", "status", "last_updated")

# Which policy slot each engine role is bound to (roles must exist for
# `autonomy_contract.validate_roles`; the V0.3 policy then selects per call).
ROLE_SLOTS = {"planner": "initial_planner", "implementer": "implementer_default",
              "self_verifier": "implementer_default", "review_prep": "review_pretreatment",
              "reviewer": "primary_reviewer", "repairer": "repair_default",
              "final_reviewer": "final_review_default"}

SLOT_LABELS = {
    "initial_planner": "Planowanie początkowe (architekt)",
    "implementer_default": "Implementacja",
    "implementer_harder": "Implementacja — trudniejsza",
    "implementer_hard": "Implementacja — bardzo trudna",
    "implementer_strong": "Implementacja — widocznie trudna (silny model od razu)",
    "implementer_capability_escalation": "Implementacja — eskalacja możliwości",
    "review_pretreatment": "Przygotowanie review",
    "primary_reviewer": "Review",
    "repair_default": "Naprawa",
    "repair_hard": "Naprawa — trudna",
    "final_review_default": "Final review",
    "final_review_hard": "Final review — wysokie ryzyko",
    "final_review_critical": "Final review — krytyczne",
    "continuation_planner": "Planowanie kolejnych iteracji (pakiet pracy)",
}


class CatalogInvalid(ValueError):
    pass


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(value)))


def validate_catalog(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise CatalogInvalid("unsupported recommendations schema")
    if not isinstance(data.get("catalog_version"), str) or not _version_key(data["catalog_version"]):
        raise CatalogInvalid("catalog_version is required")
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        raise CatalogInvalid("profiles must be an array")
    for row in profiles:
        missing = [k for k in PROFILE_FIELDS if not isinstance(row, dict) or k not in row]
        if missing:
            raise CatalogInvalid(f"profile row missing {missing}")
    choices = data.get("choices")
    if not isinstance(choices, dict) or set(CHOICE_GROUPS) - set(choices):
        raise CatalogInvalid("choices must define planning, implementation and review")
    for group in CHOICE_GROUPS:
        options = choices[group].get("options")
        if not isinstance(options, dict) or not options or choices[group].get("default") not in options:
            raise CatalogInvalid(f"choices.{group} needs options and a valid default")
        for name, option in options.items():
            slots = option.get("slots") if isinstance(option, dict) else None
            if not isinstance(slots, dict) or not slots:
                raise CatalogInvalid(f"choices.{group}.{name} needs slots")
            for slot, candidates in slots.items():
                if slot not in ap.PROFILE_KEYS:
                    raise CatalogInvalid(f"unknown policy slot {slot!r}")
                if not (isinstance(candidates, list) and candidates and all(isinstance(c, str) for c in candidates)):
                    raise CatalogInvalid(f"choices.{group}.{name}.{slot} needs candidate profile IDs")
    covered = set()
    for group in CHOICE_GROUPS:
        for option in choices[group]["options"].values():
            covered |= set(option["slots"])
    missing_slots = set(ap.PROFILE_KEYS) - covered
    if missing_slots:
        raise CatalogInvalid(f"catalog does not cover policy slots {sorted(missing_slots)}")
    mappings = data.get("exact_runtime_mappings", {})
    if not isinstance(mappings, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                 for k, v in mappings.items()):
        raise CatalogInvalid("exact_runtime_mappings must map profile IDs to profile IDs")
    return data


def exact_mappings(catalog: Mapping[str, Any]) -> dict[str, str]:
    return {k: v for k, v in (catalog.get("exact_runtime_mappings") or {}).items() if not k.startswith("_")}


def builtin_catalog() -> dict[str, Any]:
    return validate_catalog(json.loads(BUILTIN_PATH.read_text(encoding="utf-8")))


def downloaded_catalog() -> dict[str, Any] | None:
    data = product_home.read_json(product_home.home() / DOWNLOADED_NAME)
    if data is None:
        return None
    try:
        return validate_catalog(data)
    except CatalogInvalid:
        return None


def effective_catalog() -> dict[str, Any]:
    builtin = builtin_catalog()
    downloaded = downloaded_catalog()
    if downloaded and _version_key(downloaded["catalog_version"]) > _version_key(builtin["catalog_version"]):
        return {**downloaded, "_source": "DOWNLOADED"}
    return {**builtin, "_source": "BUILTIN"}


def update_catalog(url: str | None = None, *, timeout: float = 8.0,
                   fetch: Any = None) -> dict[str, Any]:
    """Fetch the catalog from GitHub. Never raises for network trouble.

    `fetch(url, timeout) -> bytes` is injectable for tests. Only a valid,
    strictly newer catalog is stored; a stored update only affects NEW runs.
    """
    builtin = builtin_catalog()
    current = effective_catalog()
    url = url or builtin.get("update_url")
    try:
        if fetch is None:
            context = ssl.create_default_context()
            request = urllib.request.Request(url, headers={"User-Agent": "AAW-Product/0.2"})
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                raw = response.read(2_000_000)
        else:
            raw = fetch(url, timeout)
        candidate = validate_catalog(json.loads(raw.decode("utf-8")))
    except CatalogInvalid as exc:
        return {"status": "REJECTED", "detail": f"pobrany katalog jest nieprawidłowy: {exc}",
                "catalog_version": current["catalog_version"], "source": current["_source"]}
    except Exception as exc:  # offline, DNS, TLS, HTTP error, bad JSON: never blocks AAW
        return {"status": "OFFLINE", "detail": f"aktualizacja niedostępna ({type(exc).__name__}); "
                f"używam katalogu {current['catalog_version']}",
                "catalog_version": current["catalog_version"], "source": current["_source"]}
    if _version_key(candidate["catalog_version"]) <= _version_key(current["catalog_version"]):
        return {"status": "UP_TO_DATE", "detail": "katalog jest aktualny",
                "catalog_version": current["catalog_version"], "source": current["_source"]}
    import datetime as dt
    stored = {**candidate, "_fetched_from": url,
              "_fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    product_home.write_json(product_home.home() / DOWNLOADED_NAME, stored)
    return {"status": "UPDATED", "detail": "nowy katalog zapisany; dotyczy tylko nowych zadań",
            "catalog_version": candidate["catalog_version"], "source": "DOWNLOADED"}


# ── resolution ───────────────────────────────────────────────────────────────

def _profiles() -> dict[str, dict[str, Any]]:
    try:
        return wr.load_implementer_profiles()
    except wr.WorkflowStop:
        return {}


def profile_display(profile_id: str | None, profiles: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    if not profile_id:
        return "—"
    row = (profiles or _profiles()).get(profile_id) or {}
    return str(row.get("display_name") or profile_id)


def resolve_implementer_chain(requested: Sequence[str] | None, *, catalog: Mapping[str, Any],
                              option: Mapping[str, Any], runnable: set[str],
                              profiles: Mapping[str, Mapping[str, Any]],
                              states: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The implementer escalation chain for ONE new run.

    `requested` is the user's own ordered chain (even a single model). Without it the
    chosen implementation level may point at the catalog's system default chain. A user
    chain is kept exactly as given (an unavailable step is reported, never replaced); the
    system default chain is used only when its first (starting) step is runnable here and
    then drops later steps that are not runnable, visibly. No chain at all (`source` None)
    leaves the legacy per-slot candidate resolution in charge.
    """
    result: dict[str, Any] = {"source": None, "profile_ids": [], "skipped": [], "blockers": [], "warnings": []}
    if requested:
        try:
            ids = ap.validate_chain(requested)
        except ValueError as exc:
            result["blockers"].append(f"łańcuch implementatora: {exc}")
            return result
        unknown = [pid for pid in ids if pid not in profiles]
        if unknown:
            result["blockers"].append(f"łańcuch implementatora: nieznane profile {', '.join(unknown)}")
            return result
        local = [pid for pid in ids if profiles[pid].get("not_implementer")]
        if local:
            result["blockers"].append("łańcuch implementatora: profile lokalne nie mogą implementować: " + ", ".join(local))
            return result
        result.update(source="USER", profile_ids=ids)
        return result
    spec = catalog.get("default_implementer_chain")
    if option.get("implementer_chain") != "DEFAULT" or not isinstance(spec, Mapping):
        return result
    try:
        ids = ap.validate_chain(spec.get("profiles"))
    except ValueError:
        return result
    kept = [pid for pid in ids if pid in runnable and pid in profiles]
    # Leading steps of a provider whose CLI is not installed at all are not a "different model" the run could
    # silently start on: the chain simply begins at the first step that this machine can have (Claude-only setup).
    start = next((i for i, pid in enumerate(ids) if (states.get(pid) or {}).get("state") != "CLI_NOT_FOUND"), 0)
    ids_from_start = ids[start:]
    for pid in ids:
        if pid not in kept and (states.get(pid) or {}).get("state") != "CLI_NOT_FOUND":   # uninstalled CLI: not noise
            state = states.get(pid) or {}
            result["skipped"].append({"profile_id": pid, "display": profile_display(pid, profiles),
                                      "state": state.get("state"), "reason": state.get("reason") or state.get("label"),
                                      "checkable": state.get("state") in ("NEEDS_CHECK", "NOT_VERIFIED")})
    if ids_from_start[0] not in kept:
        # The chain's starting model is not runnable here (no CLI, rejected by a local probe, ...): starting
        # silently on a later, different step would be a substitution. The per-slot candidates decide instead,
        # with their visible ALTERNATIVE / UNAVAILABLE statuses (e.g. a Claude-only setup before verification).
        return result
    if result["skipped"]:
        result["warnings"].append("Pominięto w domyślnym łańcuchu implementatora (niedostępne na tym komputerze): " +
                                  ", ".join(f"{row['display']}" for row in result["skipped"]) +
                                  ". Krok niedostępny jest pomijany jawnie — nigdy podmieniany na inny model.")
    result.update(source="DEFAULT", profile_ids=kept)
    return result


def resolve_choices(choices: Mapping[str, str], *, runnable: set[str], catalog: Mapping[str, Any] | None = None,
                    overrides: Mapping[str, str] | None = None,
                    detection: Mapping[str, Any] | None = None,
                    states: Mapping[str, Mapping[str, Any]] | None = None,
                    implementer_chain: Sequence[str] | None = None) -> dict[str, Any]:
    """Map the three simple levels onto every V0.3 policy slot for one new run.

    `implementer_chain` (optional) is the user's own ordered implementer escalation chain;
    without it the implementation level's system default chain (if any) is used.
    """
    catalog = catalog or effective_catalog()
    profiles = _profiles()
    mappings = exact_mappings(catalog)
    states = states or {}
    overrides = dict(overrides or {})
    picked: dict[str, str] = {}
    for group in CHOICE_GROUPS:
        value = choices.get(group) or catalog["choices"][group]["default"]
        if value not in catalog["choices"][group]["options"]:
            raise ValueError(f"unknown {group} level {value!r}")
        picked[group] = value
    slot_candidates: dict[str, list[str]] = {}
    for group in CHOICE_GROUPS:
        slot_candidates.update(catalog["choices"][group]["options"][picked[group]]["slots"])
    for group in CHOICE_GROUPS:  # a slot the chosen option does not name falls back to the group default
        default = catalog["choices"][group]["options"][catalog["choices"][group]["default"]]["slots"]
        for slot, candidates in default.items():
            slot_candidates.setdefault(slot, candidates)
    required = set(catalog.get("required_slots") or [])
    meta = {row["profile"]: row for row in catalog.get("profiles", [])}
    versions = {p["harness"]: p.get("version") for p in (detection or {}).get("providers", [])}

    def runtime(profile_id: str) -> str | None:
        return (profiles.get(profile_id) or {}).get("runtime_model_id")

    slots: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    warnings: list[str] = []
    order = list(ap.PROFILE_KEYS)
    implementer_model = None
    chain = resolve_implementer_chain(
        implementer_chain, catalog=catalog,
        option=catalog["choices"]["implementation"]["options"][picked["implementation"]],
        runnable=runnable, profiles=profiles, states=states)
    blockers.extend(chain["blockers"])
    warnings.extend(chain["warnings"])
    chain_ids = chain["profile_ids"] or None
    chain_slots = ap.chain_slot_ids(chain_ids) if chain_ids else {}
    chain_status = "RECOMMENDED" if chain["source"] == "DEFAULT" else "OVERRIDE"
    for slot in order:
        candidates = list(slot_candidates[slot])
        recommended = candidates[0]
        status, chosen, reason, mapped_from = None, None, None, None
        if slot in chain_slots:
            chosen, status = chain_slots[slot], chain_status
            recommended = chosen
            index = ap.CHAIN_SLOT_INDEX[slot]
            reason = (f"krok {min(index, len(chain_ids) - 1) + 1} z {len(chain_ids)} łańcucha implementatora"
                      + (" (domyślny łańcuch AAW)" if chain["source"] == "DEFAULT" else " (wybrany ręcznie)"))
            if slot in overrides:
                blockers.append(f"{SLOT_LABELS[slot]}: ręczny profil {overrides[slot]} koliduje z łańcuchem "
                                "implementatora — usuń ręczny profil albo zmień łańcuch")
            elif chosen not in runnable:
                if slot in required:
                    blockers.append(f"{SLOT_LABELS[slot]}: {chosen} (krok łańcucha) nie jest teraz dostępny na tym "
                                    "komputerze — ten krok służy też do napraw i przygotowania review, więc musi działać")
                else:
                    warnings.append(f"{SLOT_LABELS[slot]}: {chosen} (krok łańcucha) nie jest teraz dostępny — jeśli "
                                    "eskalacja do niego będzie potrzebna, AAW zatrzyma się i poprosi o decyzję")
        elif slot in overrides:
            chosen, status = overrides[slot], "OVERRIDE"
            if chosen not in profiles:
                blockers.append(f"{SLOT_LABELS[slot]}: nieznany profil {chosen}")
            elif chosen not in runnable:
                (blockers if slot in required else warnings).append(
                    f"{SLOT_LABELS[slot]}: wybrany ręcznie profil {chosen} nie jest teraz dostępny")
        else:
            # Each candidate is served by itself or, when the frozen catalog cannot run it, by its exact
            # runtime mapping (same model and effort) once the local probe confirmed that mapping.
            served = []
            for candidate in candidates:
                if candidate in runnable:
                    served.append((candidate, candidate))
                elif mappings.get(candidate) in runnable:
                    served.append((mappings[candidate], candidate))
            if slot in ("primary_reviewer", "final_review_default") and implementer_model:
                independent = [pair for pair in served if runtime(pair[0]) != implementer_model]
                served = independent or served
            if served:
                chosen, source = served[0]
                status = "RECOMMENDED" if source == recommended else "ALTERNATIVE"
                if chosen != source:
                    mapped_from = source
                    reason = (f"dokładne mapowanie {source} → {runtime(chosen)} / "
                              f"{(profiles.get(chosen) or {}).get('effort')} (sprawdzone na tym komputerze)")
                if status == "ALTERNATIVE":
                    why = _why_unavailable(recommended, mappings, states)
                    reason = f"{recommended} niedostępny na tym komputerze" + (f" — {why}" if why else "") + \
                        (f"; {reason}" if reason else "")
            else:
                chosen, status = recommended, "UNAVAILABLE"
                if slot in required:
                    blockers.append(f"{SLOT_LABELS[slot]}: żaden rekomendowany profil nie jest dostępny "
                                    f"({', '.join(candidates)}) — wybierz inny poziom w kroku „Modele” albo "
                                    "zainstaluj i zaloguj inne CLI")
                else:
                    warnings.append(f"{SLOT_LABELS[slot]}: profil {recommended} niedostępny — jeśli ta "
                                    "eskalacja będzie potrzebna, AAW zatrzyma się i poprosi o decyzję")
        row_meta = meta.get(chosen) or {}
        minimum = row_meta.get("minimum_cli_version")
        harness = (profiles.get(chosen) or {}).get("harness")
        if minimum and harness in versions:
            from product_providers import compare_versions
            if compare_versions(versions[harness], minimum) is False:
                warnings.append(f"{SLOT_LABELS[slot]}: {chosen} wymaga {harness} CLI ≥ {minimum} "
                                f"(wykryto {versions[harness]})")
        mapping = mappings.get(recommended)
        can_enable = bool(mapping and mapping != chosen and (states.get(mapping) or {}).get("state") in
                          ("NEEDS_CHECK", "NOT_VERIFIED"))
        slots[slot] = {"slot": slot, "label": SLOT_LABELS[slot], "profile_id": chosen,
                       "display": profile_display(chosen, profiles), "recommended_profile_id": recommended,
                       "exact_mapping_of": mapped_from,
                       "availability": (states.get(chosen) or {}).get("state"),
                       "recommended_check_available": can_enable, "recommended_check_profile": mapping if can_enable else None,
                       "status": status, "reason": reason, "required": slot in required,
                       "harness": harness, "runtime_model_id": runtime(chosen),
                       "effort": (profiles.get(chosen) or {}).get("effort"),
                       "strength_class": row_meta.get("strength_class"),
                       "speed_class": row_meta.get("speed_class"), "cost_class": row_meta.get("cost_class")}
        if slot == "implementer_default":
            implementer_model = runtime(chosen)
    unverified = sorted({s["display"] for s in slots.values() if s["required"] and s["availability"] == "NOT_VERIFIED"})
    if unverified:
        warnings.append("Jeszcze nie sprawdzono na tym komputerze: " + ", ".join(unverified) +
                        ". Katalog AAW uznaje je za dostępne; „Sprawdź modele” potwierdzi to jednym krótkim "
                        "zapytaniem na model. Jeśli konto nie ma dostępu, AAW zatrzyma się zamiast podmienić model.")
    alternatives = [s for s in slots.values() if s["status"] == "ALTERNATIVE"]
    if alternatives:
        warnings.insert(0, f"{len(alternatives)} z {len(slots)} etapów użyje jawnej alternatywy, bo rekomendowany "
                        "profil jest niedostępny na tym komputerze (każda alternatywa jest opisana przy swoim etapie).")
    roles = {role: {"profile_id": slots[slot]["profile_id"]} for role, slot in ROLE_SLOTS.items()}
    same_model = runtime(roles["reviewer"]["profile_id"]) == runtime(roles["implementer"]["profile_id"])
    if same_model:
        warnings.append("Review używa tego samego modelu co implementacja (świeży kontekst, słabsza niezależność).")
    config = {"contract": ap.POLICY_VERSION, "preset_id": ap.PRESET_ID,
              "purpose": "AAW Product run binding resolved from simple levels; frozen by the engine at start.",
              "allow_same_model_fresh_context": same_model, "roles": roles,
              "policy_profiles": {slot: slots[slot]["profile_id"] for slot in ap.PROFILE_KEYS}}
    chain_steps: list[dict[str, Any]] = []
    if chain_ids:
        config[ap.CHAIN_KEY] = list(chain_ids)
        for position, pid in enumerate(chain_ids, start=1):
            state = states.get(pid) or {}
            chain_steps.append({"step": position, "profile_id": pid, "display": profile_display(pid, profiles),
                                "runnable": pid in runnable, "availability": state.get("state"),
                                "availability_label": state.get("label"), "reason": state.get("reason"),
                                "harness": (profiles.get(pid) or {}).get("harness"),
                                "runtime_model_id": runtime(pid), "effort": (profiles.get(pid) or {}).get("effort"),
                                "checkable": state.get("state") in ("NEEDS_CHECK", "NOT_VERIFIED")})
            if pid not in runnable and pid not in {slots[k]["profile_id"] for k in chain_slots}:
                warnings.append(f"Krok {position} łańcucha implementatora ({profile_display(pid, profiles)}) "
                                "nie jest teraz dostępny — jeśli eskalacja do niego będzie potrzebna, AAW zatrzyma "
                                "się i poprosi o decyzję zamiast podmienić model.")
        reviewer_model = runtime(roles["reviewer"]["profile_id"])
        if reviewer_model and reviewer_model in {runtime(pid) for pid in chain_ids[1:]} and not same_model:
            warnings.append("Review używa tego samego modelu co jeden z dalszych kroków łańcucha implementatora "
                            "(świeży kontekst, słabsza niezależność, jeśli eskalacja do niego nastąpi).")
    routing = _routing_defaults()
    if routing:
        # Quota/trust/capability routing and provider failover ship with the engine's defaults; the repair
        # escalation ladder is derived from the policy profiles above (autonomy_policy.default_repair_escalation).
        config["routing"] = _routing_for_machine(routing, runnable)
    chain_defaults = _chain_defaults()
    if chain_defaults:
        config["chain"] = chain_defaults        # chain mode (autonomy_chain): long chains, one serious review
    validated = None
    try:
        validated = ac.validate_roles(config, profiles)
    except ac.AutonomyError as exc:
        blockers.append(f"konfiguracja ról odrzucona przez silnik: {exc}")
    return {"choices": picked, "catalog_version": catalog["catalog_version"],
            "catalog_source": catalog.get("_source", "BUILTIN"), "slots": slots,
            "roles_config": config, "roles_valid": validated is not None,
            "implementer_chain": {"source": chain["source"] or "SLOTS", "steps": chain_steps,
                                  "skipped": chain["skipped"],
                                  "default_profile_ids": list((catalog.get("default_implementer_chain") or {})
                                                              .get("profiles") or [])},
            "blockers": blockers, "warnings": warnings}


def _routing_defaults() -> dict[str, Any] | None:
    """The `routing` block of AUTONOMY_ROLES.json (None if the file or block is missing: routing stays off)."""
    try:
        data = json.loads((Path(__file__).resolve().parent / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    routing = data.get("routing")
    return routing if isinstance(routing, dict) else None


def _chain_defaults() -> dict[str, Any] | None:
    """The `chain` block of AUTONOMY_ROLES.json (None if absent: the classic per-iteration cycle runs)."""
    try:
        data = json.loads((Path(__file__).resolve().parent / "AUTONOMY_ROLES.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    chain = data.get("chain")
    return chain if isinstance(chain, dict) else None


def _routing_for_machine(routing: Mapping[str, Any], runnable: set[str]) -> dict[str, Any]:
    """Routing alternatives limited to what this machine can run now.

    The router never probes an alternative up front, so a profile that is
    installed but not verified here (e.g. Gemini before „Sprawdź modele”) must
    not be offered to it; a probe-confirmed one is enabled even if the shipped
    routing block declares it unavailable by default.
    """
    out = json.loads(json.dumps(routing))
    for profile_id, row in (out.get("profiles") or {}).items():
        if not isinstance(row, dict):
            continue
        if profile_id in runnable:
            row["available"] = True
            row.pop("unavailable_reason", None)
        else:
            row["available"] = False
            row.setdefault("unavailable_reason", "not runnable on this machine (AAW product detection/probe)")
    return out


def _why_unavailable(profile_id: str, mappings: Mapping[str, str], states: Mapping[str, Mapping[str, Any]]) -> str | None:
    own = states.get(profile_id) or {}
    mapped = states.get(mappings.get(profile_id) or "") or {}
    if mapped.get("state") == "NEEDS_CHECK":
        return f"można go włączyć: „Sprawdź modele” zweryfikuje {mapped.get('runtime_model_id')} na tym komputerze"
    if mapped.get("state") == "REJECTED_HERE":
        return f"CLI odrzuciło {mapped.get('runtime_model_id')} na tym komputerze"
    if own.get("state") in ("CLI_NOT_FOUND", "NOT_LOGGED_IN", "REJECTED_HERE"):
        return own.get("label")
    return None


GROUP_SLOTS = {"planning": ("initial_planner", "continuation_planner"),
               "implementation": ("implementer_default", "repair_default", "review_pretreatment",
                                  "implementer_harder", "implementer_hard", "implementer_strong", "repair_hard",
                                  "implementer_capability_escalation"),
               "review": ("primary_reviewer", "final_review_default", "final_review_hard", "final_review_critical")}
GROUP_MAIN_SLOTS = {"planning": ("initial_planner",),
                    "implementation": ("implementer_default", "repair_default"),
                    "review": ("primary_reviewer", "final_review_default")}


def group_summary(resolution: Mapping[str, Any]) -> dict[str, Any]:
    """Per simple level: the actual models that will run (main slots) — shown under the three choices."""
    out = {}
    for group, slots in GROUP_SLOTS.items():
        rows = [resolution["slots"][s] for s in slots]
        main = [resolution["slots"][s] for s in GROUP_MAIN_SLOTS[group]]
        chain_steps = (resolution.get("implementer_chain") or {}).get("steps") if group == "implementation" else None
        models = ([row["display"] for row in chain_steps] if chain_steps
                  else list(dict.fromkeys(f"{s['display']}" for s in main)))
        out[group] = {"choice": resolution["choices"][group],
                      "models": models,
                      "main": main, "all": rows,
                      "alternatives": [s for s in rows if s["status"] == "ALTERNATIVE"],
                      "unavailable": [s for s in rows if s["status"] == "UNAVAILABLE"],
                      "checkable": sorted({s["recommended_check_profile"] for s in rows
                                           if s.get("recommended_check_profile")} |
                                          {s["profile_id"] for s in rows if s.get("availability") == "NOT_VERIFIED"} |
                                          ({row["profile_id"] for row in chain_steps if row["checkable"]}
                                           if chain_steps else set()) |
                                          ({row["profile_id"] for row in (resolution.get("implementer_chain") or {})
                                            .get("skipped", []) if row.get("checkable")}
                                           if group == "implementation" else set()))}
    return out


def choice_options(catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    catalog = catalog or effective_catalog()
    return {group: {"label": catalog["choices"][group].get("label", group),
                    "default": catalog["choices"][group]["default"],
                    "options": [{"value": key, "label": opt.get("label", key), "description": opt.get("description")}
                                for key, opt in catalog["choices"][group]["options"].items()]}
            for group in CHOICE_GROUPS}
