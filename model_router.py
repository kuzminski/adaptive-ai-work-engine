#!/usr/bin/env python3
"""AAW MODEL ROUTER V0.1 — quota-, trust- and capability-aware profile routing.

This extends the existing profile-selection layer (`autonomy_policy.select_*`
picks the profile the *policy* wants; `AutonomyController._call` resolves and
dispatches it). It does not replace it: the policy's choice stays the
PREFERRED profile, and this module only decides whether a different, equally
adequate profile should serve the call instead — and records why.

Pure and deterministic: no clock (callers pass `now`), no filesystem, no model
calls. The same inputs always produce the same decision, so every routing
decision can be recomputed from the audit record it returns.

Order of concerns (quality first, quota second):

  1. hard filters   — availability, trust level, required capabilities,
                      capability class floor, provider health, exhausted limit;
  2. preferred stays — while the preferred profile has normal headroom (or its
                      telemetry is UNKNOWN) nothing else is considered;
  3. quota optimisation — only when the preferred profile is LOW / in RESERVE /
                      ineligible, and only among profiles that passed (1);
  4. hysteresis     — after a quota-driven switch the original provider is not
                      returned to until it recovers (>= return threshold), a
                      reset is confirmed, or nothing better exists.

Telemetry certainty is never invented: an UNKNOWN limit carries no percent;
an ESTIMATED one must carry provenance or it is demoted to UNKNOWN; stale
EXACT values are demoted to ESTIMATED (and eventually UNKNOWN).

The most restrictive active limit of a provider decides its zone, so several
simultaneous limits (short window, 5h, daily, weekly, cost, per-model, rate
limit) are handled uniformly.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any, Mapping, Sequence

ROUTER_VERSION = "AAW_MODEL_ROUTER_V0.1"

# ── vocabularies ─────────────────────────────────────────────────────────────

EXACT, ESTIMATED, UNKNOWN = "EXACT", "ESTIMATED", "UNKNOWN"
CERTAINTIES = (EXACT, ESTIMATED, UNKNOWN)
PRODUCTION, SECONDARY, EXPERIMENTAL, DISABLED = "PRODUCTION", "SECONDARY", "EXPERIMENTAL", "DISABLED"
TRUSTS = (PRODUCTION, SECONDARY, EXPERIMENTAL, DISABLED)
SMALL, MEDIUM, LARGE, VERY_LARGE = "SMALL", "MEDIUM", "LARGE", "VERY_LARGE"
TASK_CLASSES = (SMALL, MEDIUM, LARGE, VERY_LARGE)
NORMAL_CRITICALITY, CRITICAL = "NORMAL", "CRITICAL"

Z_NORMAL, Z_LOW, Z_RESERVE, Z_EXHAUSTED, Z_UNKNOWN = "NORMAL", "LOW", "RESERVE", "EXHAUSTED", "UNKNOWN"

# provider failure classes (hard signals; independent of quota estimates)
F_RATE_LIMIT, F_TIMEOUT, F_AUTH, F_UNAVAILABLE, F_OTHER = "RATE_LIMIT", "TIMEOUT", "AUTH", "UNAVAILABLE", "OTHER"
FAILURE_CLASSES = (F_RATE_LIMIT, F_TIMEOUT, F_AUTH, F_UNAVAILABLE, F_OTHER)

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "soft_threshold_percent": 10,
    "reserve_percent": 5,
    "near_reset_minutes": 20,
    "return_threshold_percent": 25,
    "near_reset_max_task_class": SMALL,
    # Reserve is protected for these phases; ordinary EXECUTE/PLAN do not draw on it.
    "reserve_allowed_phases": ["review", "repair", "final_review", "diagnose"],
    "reserve_last_resort": False,
    # Larger tasks need more headroom before the provider counts as "normal".
    "size_headroom_percent": {SMALL: 0, MEDIUM: 0, LARGE: 5, VERY_LARGE: 15},
    # Minimum capability class a profile must declare to take a task of this size.
    "task_class_floor": {SMALL: 1, MEDIUM: 2, LARGE: 3, VERY_LARGE: 4},
    "phase_class_floor": {},
    "switch_penalty": 10,
    "switch_penalty_task_multiplier": {SMALL: 0.5, MEDIUM: 1.0, LARGE: 1.5, VERY_LARGE: 2.0},
    "experimental": {"allowed_phases": ["execute", "repair", "self_verify", "diagnose"],
                     "allowed_task_classes": [SMALL, MEDIUM], "allow_critical": False},
    "secondary": {"disallowed_phases_when_critical": ["final_review"]},
    "telemetry": {"stale_after_minutes": 30, "unknown_after_minutes": 180},
    "health": {"timeout_cooldown_minutes": 10, "rate_limit_cooldown_minutes": 15,
               "auth_cooldown_minutes": 60, "unavailable_cooldown_minutes": 5},
    "demotion_ttl_minutes": 300,
}


class RouterError(ValueError):
    """Fail-closed router configuration error."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RouterError(message)


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(base))
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, Mapping) and isinstance(out.get(key), Mapping) \
            else copy.deepcopy(value)
    return out


def normalize_policy(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    pol = _merge(DEFAULT_POLICY, overrides or {})
    for key in ("soft_threshold_percent", "reserve_percent", "return_threshold_percent", "near_reset_minutes"):
        value = pol[key]
        limit = 100 if key.endswith("percent") else float("inf")
        _require(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= limit,
                 f"policy.{key} must be a non-negative number (percent values <= 100)")
    _require(pol["reserve_percent"] < pol["soft_threshold_percent"] <= pol["return_threshold_percent"],
             "policy needs reserve_percent < soft_threshold_percent <= return_threshold_percent")
    _require(pol["near_reset_max_task_class"] in TASK_CLASSES, "policy.near_reset_max_task_class is not a task class")
    return pol


# ── time ─────────────────────────────────────────────────────────────────────

def parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _minutes(delta: dt.timedelta) -> float:
    return round(delta.total_seconds() / 60.0, 2)


# ── telemetry ────────────────────────────────────────────────────────────────

def normalize_limit(raw: Mapping[str, Any], *, now: dt.datetime, policy: Mapping[str, Any],
                    pool_observed_at: Any = None) -> dict[str, Any]:
    """One limit, with honest certainty. Never fabricates a percentage."""
    notes: list[str] = []
    certainty = str(raw.get("certainty") or UNKNOWN).upper()
    if certainty not in CERTAINTIES:
        notes.append(f"unrecognised certainty {raw.get('certainty')!r} treated as UNKNOWN")
        certainty = UNKNOWN
    percent = raw.get("remaining_percent")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
        if percent is not None:
            notes.append("remaining_percent outside 0..100 or non-numeric; discarded")
        percent = None
    provenance = str(raw.get("provenance") or raw.get("source") or "").strip()
    if certainty == UNKNOWN and percent is not None:
        notes.append("percent supplied with UNKNOWN certainty; discarded (no invented values)")
        percent = None
    if certainty == ESTIMATED and not provenance:
        notes.append("ESTIMATED without provenance; demoted to UNKNOWN")
        certainty, percent = UNKNOWN, None
    if certainty != UNKNOWN and percent is None and not raw.get("blocked"):
        notes.append("no usable percent; treated as UNKNOWN")
        certainty = UNKNOWN
    observed = parse_time(raw.get("observed_at")) or parse_time(pool_observed_at)
    if observed is not None and certainty != UNKNOWN:
        age = _minutes(now - observed)
        tel = policy["telemetry"]
        if age > tel["unknown_after_minutes"]:
            notes.append(f"telemetry {age}m old; no longer trusted")
            certainty, percent = UNKNOWN, None
        elif age > tel["stale_after_minutes"] and certainty == EXACT:
            notes.append(f"telemetry {age}m old; EXACT demoted to ESTIMATED")
            certainty = ESTIMATED
            provenance = (provenance + "; " if provenance else "") + f"stale:{age}m"
    resets_at = parse_time(raw.get("resets_at"))
    resets_in = raw.get("resets_in_minutes")
    if resets_at is not None:
        resets_in = max(0.0, _minutes(resets_at - now))
    elif isinstance(resets_in, (int, float)) and not isinstance(resets_in, bool) and resets_in >= 0:
        resets_in = float(resets_in)
    else:
        resets_in = None
    retry = raw.get("retry_after_minutes")
    retry = float(retry) if isinstance(retry, (int, float)) and not isinstance(retry, bool) and retry >= 0 else None
    return {"window": str(raw.get("window") or raw.get("kind") or "limit"),
            "kind": str(raw.get("kind") or "WINDOW").upper(), "model": raw.get("model"),
            "certainty": certainty, "remaining_percent": percent, "resets_in_minutes": resets_in,
            "blocked": bool(raw.get("blocked")), "retry_after_minutes": retry,
            "last_reset_at": _iso(parse_time(raw["last_reset_at"])) if parse_time(raw.get("last_reset_at")) else None,
            "provenance": provenance or None, "notes": notes}


def quota_view(pool_telemetry: Mapping[str, Any] | None, *, model_id: str | None, now: dt.datetime,
               policy: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse every active limit of one provider into the most restrictive one."""
    raw_limits = list((pool_telemetry or {}).get("limits") or [])
    observed = (pool_telemetry or {}).get("observed_at")
    limits = [normalize_limit(row, now=now, policy=policy, pool_observed_at=observed)
              for row in raw_limits if isinstance(row, Mapping)
              and (row.get("model") in (None, "", model_id))]
    known = [l for l in limits if l["remaining_percent"] is not None]
    blocked = [l for l in limits if l["blocked"]]
    binding: dict[str, Any] | None = None
    if blocked:
        # a blocked pool is unusable until its longest block lifts
        binding = max(blocked, key=lambda l: (l["retry_after_minutes"] or l["resets_in_minutes"] or 0))
    elif known:
        binding = min(known, key=lambda l: (l["remaining_percent"], CERTAINTIES.index(l["certainty"])))
    return {
        "percent": None if binding is None or binding["blocked"] else binding["remaining_percent"],
        "certainty": binding["certainty"] if binding else UNKNOWN,
        "binding_limit": binding["window"] if binding else None,
        "resets_in_minutes": (binding["retry_after_minutes"] if binding and binding["retry_after_minutes"] is not None
                              else binding["resets_in_minutes"]) if binding else None,
        "blocked": bool(blocked),
        "limits": limits,
        "unknown_windows": [l["window"] for l in limits if l["remaining_percent"] is None and not l["blocked"]],
        "last_reset_at": max((l["last_reset_at"] for l in limits if l["last_reset_at"]), default=None),
    }


def load_telemetry(path: Any) -> dict[str, Any]:
    """Read the operator/adapter-maintained telemetry file. Missing or malformed means UNKNOWN, never an error."""
    import json
    from pathlib import Path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("pools"), dict) else {}


def zone_for(view: Mapping[str, Any], policy: Mapping[str, Any], task_class: str) -> str:
    if view["blocked"]:
        return Z_EXHAUSTED
    percent = view["percent"]
    if percent is None:
        return Z_UNKNOWN
    if percent <= 0:
        return Z_EXHAUSTED if view["certainty"] == EXACT else Z_RESERVE
    if percent <= policy["reserve_percent"]:
        return Z_RESERVE
    soft = policy["soft_threshold_percent"] + policy["size_headroom_percent"].get(task_class, 0)
    return Z_LOW if percent < soft else Z_NORMAL


# ── provider health (hard signals) ───────────────────────────────────────────

def empty_state() -> dict[str, Any]:
    return {"demoted": {}, "health": {}}


def record_failure(state: Mapping[str, Any] | None, pool: str, failure_class: str, *, now: dt.datetime,
                   retry_after_minutes: float | None = None, policy: Mapping[str, Any] | None = None,
                   detail: str | None = None) -> dict[str, Any]:
    """Mark a provider unhealthy after a hard failure. Returns a new state."""
    pol = normalize_policy(policy)
    out = copy.deepcopy(dict(state or empty_state()))
    out.setdefault("demoted", {})
    out.setdefault("health", {})
    if failure_class not in FAILURE_CLASSES or failure_class == F_OTHER:
        return out
    cooldown = {F_RATE_LIMIT: pol["health"]["rate_limit_cooldown_minutes"],
                F_TIMEOUT: pol["health"]["timeout_cooldown_minutes"],
                F_AUTH: pol["health"]["auth_cooldown_minutes"],
                F_UNAVAILABLE: pol["health"]["unavailable_cooldown_minutes"]}[failure_class]
    if failure_class == F_RATE_LIMIT and retry_after_minutes is not None:
        cooldown = retry_after_minutes
    previous = out["health"].get(pool) or {}
    out["health"][pool] = {"status": failure_class, "since": _iso(now),
                           "until": _iso(now + dt.timedelta(minutes=cooldown)),
                           "failures": int(previous.get("failures", 0)) + 1, "detail": (detail or "")[:300] or None}
    return out


def record_success(state: Mapping[str, Any] | None, pool: str) -> dict[str, Any]:
    out = copy.deepcopy(dict(state or empty_state()))
    out.setdefault("health", {}).pop(pool, None)
    out.setdefault("demoted", {})
    return out


def _health_block(state: Mapping[str, Any], pool: str, now: dt.datetime) -> dict[str, Any] | None:
    row = (state.get("health") or {}).get(pool)
    until = parse_time((row or {}).get("until"))
    if row and until is not None and until > now:
        return {"status": row["status"], "until": row["until"], "minutes_left": _minutes(until - now)}
    return None


# ── request / candidates ─────────────────────────────────────────────────────

def _task_rank(task_class: str) -> int:
    return TASK_CLASSES.index(task_class)


def normalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
    task_class = str(request.get("task_class") or MEDIUM).upper()
    _require(task_class in TASK_CLASSES, f"unknown task_class {task_class!r}")
    criticality = str(request.get("criticality") or NORMAL_CRITICALITY).upper()
    _require(criticality in (NORMAL_CRITICALITY, CRITICAL), f"unknown criticality {criticality!r}")
    return {"phase": str(request.get("phase") or "execute"), "role": request.get("role"),
            "task_class": task_class, "criticality": criticality,
            "required_capabilities": sorted(set(request.get("required_capabilities") or [])),
            "min_capability_class": request.get("min_capability_class"),
            "preferred_profile_id": request.get("preferred_profile_id"),
            "current_pool": request.get("current_pool"), "current_profile_id": request.get("current_profile_id"),
            "operation_in_progress": bool(request.get("operation_in_progress")),
            "repo_modifying": bool(request.get("repo_modifying")),
            "manual_override": request.get("manual_override"),
            "exclude_profiles": sorted(set(request.get("exclude_profiles") or [])),
            "exclude_models": sorted(set(request.get("exclude_models") or []))}


def _filters(cand: Mapping[str, Any], req: Mapping[str, Any], pol: Mapping[str, Any], floor: int,
             health: Mapping[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    trust = str(cand.get("trust") or SECONDARY).upper()
    if trust == DISABLED:
        reasons.append("TRUST_DISABLED")
    if not cand.get("available", True):
        reasons.append("NOT_AVAILABLE:" + str(cand.get("unavailable_reason") or "unavailable"))
    if cand["profile_id"] in req["exclude_profiles"] or cand.get("runtime_model_id") in req["exclude_models"]:
        reasons.append("EXCLUDED")
    missing = sorted(set(req["required_capabilities"]) - set(cand.get("capabilities") or ()))
    if missing:
        reasons.append("CAPABILITY_MISMATCH:" + ",".join(missing))
    if int(cand.get("capability_class", 0)) < floor:
        reasons.append(f"CLASS_BELOW_FLOOR:{cand.get('capability_class', 0)}<{floor}")
    if trust == EXPERIMENTAL:
        ex = pol["experimental"]
        if req["phase"] not in ex["allowed_phases"]:
            reasons.append("EXPERIMENTAL_PHASE_NOT_ALLOWED")
        if req["task_class"] not in ex["allowed_task_classes"]:
            reasons.append("EXPERIMENTAL_TASK_CLASS_NOT_ALLOWED")
        if req["criticality"] == CRITICAL and not ex["allow_critical"]:
            reasons.append("EXPERIMENTAL_NOT_FOR_CRITICAL")
    if trust == SECONDARY and req["criticality"] == CRITICAL and \
            req["phase"] in pol["secondary"]["disallowed_phases_when_critical"]:
        reasons.append("SECONDARY_NOT_FOR_CRITICAL_PHASE")
    if health:
        reasons.append(f"PROVIDER_BLOCKED:{health['status']}")
    return reasons


def _return_confirmed(entry: Mapping[str, Any], view: Mapping[str, Any], pol: Mapping[str, Any],
                      now: dt.datetime) -> str | None:
    """Why a demoted provider may be used again, or None while hysteresis holds."""
    percent = view["percent"]
    if percent is not None and view["certainty"] != UNKNOWN and percent >= pol["return_threshold_percent"]:
        return "RECOVERED_ABOVE_RETURN_THRESHOLD"
    since = parse_time(entry.get("since"))
    reset = parse_time(view.get("last_reset_at"))
    if since is not None and reset is not None and reset > since:
        return "RESET_CONFIRMED"
    if since is not None and _minutes(now - since) >= pol["demotion_ttl_minutes"] and percent is None:
        return "DEMOTION_EXPIRED_NO_TELEMETRY"
    return None


# ── scoring (used only to rank adequate alternatives) ────────────────────────

_TRUST_POINTS = {PRODUCTION: 20.0, SECONDARY: 8.0, EXPERIMENTAL: -10.0}
_ZONE_POINTS = {Z_NORMAL: 0.0, Z_UNKNOWN: 0.0, Z_LOW: -60.0, Z_RESERVE: -120.0, Z_EXHAUSTED: -1000.0}


def score_candidate(ev: Mapping[str, Any], req: Mapping[str, Any], pol: Mapping[str, Any], floor: int,
                    preferred_id: str | None, preferred_class: int | None = None) -> dict[str, Any]:
    """Audit-friendly score. Capability dominates; quota never lifts a weak model over a strong one.

    Quality is measured against the profile the policy wanted: each class step
    *below* it costs 40 points (lost quality), each step above 15 (wasted
    strength). Quota headroom contributes at most 30 points, so it can only
    choose among comparable profiles.
    """
    comps: dict[str, float] = {}
    gap = int(ev["capability_class"]) - int(preferred_class if preferred_class is not None else floor)
    comps["capability_overshoot"] = -15.0 * gap if gap > 0 else 40.0 * gap
    comps["trust"] = _TRUST_POINTS.get(ev["trust"], 0.0)
    comps["zone"] = _ZONE_POINTS[ev["zone"]]
    view = ev["quota"]
    percent = view["percent"]
    factor = {EXACT: 0.30, ESTIMATED: 0.18}.get(view["certainty"], 0.0)
    comps["quota_headroom"] = round((percent or 0.0) * factor, 2) if percent is not None else 0.0
    reset = view["resets_in_minutes"]
    comps["near_reset"] = 5.0 if ev["zone"] in (Z_LOW, Z_RESERVE) and reset is not None \
        and reset <= pol["near_reset_minutes"] else 0.0
    multiplier = pol["switch_penalty_task_multiplier"].get(req["task_class"], 1.0)
    switching = req["current_pool"] is not None and ev["pool"] != req["current_pool"]
    comps["switch_penalty"] = -round(pol["switch_penalty"] * multiplier, 2) if switching else 0.0
    comps["cost"] = -round((float(ev.get("cost_weight", 1.0)) - 1.0) * 10.0 * multiplier, 2)
    comps["policy_preferred"] = 15.0 if ev["profile_id"] == preferred_id else 0.0
    return {"total": round(sum(comps.values()), 2), "components": comps}


# ── the decision ─────────────────────────────────────────────────────────────

def route(request: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]],
          telemetry: Mapping[str, Any] | None = None, router_state: Mapping[str, Any] | None = None, *,
          policy: Mapping[str, Any] | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Pick the profile to run and a ranked failover list.

    `candidates` rows: profile_id, pool (provider quota pool), runtime_model_id,
    trust, capability_class (int), capabilities, available, unavailable_reason,
    cost_weight. `telemetry`: {"pools": {pool: {"limits": [...], "observed_at"}}}.
    The returned decision is the complete audit record.
    """
    pol = normalize_policy(policy)
    req = normalize_request(request)
    now = now or dt.datetime.now(dt.timezone.utc)
    state = copy.deepcopy(dict(router_state or empty_state()))
    state.setdefault("demoted", {})
    state.setdefault("health", {})
    by_id = {c["profile_id"]: dict(c) for c in candidates}
    preferred_id = req["preferred_profile_id"]
    pools = (telemetry or {}).get("pools") or {}

    floor = max(int(pol["task_class_floor"].get(req["task_class"], 0)),
                int(pol["phase_class_floor"].get(req["phase"], 0)),
                int(req["min_capability_class"] or 0))
    evals: dict[str, dict[str, Any]] = {}
    for pid, cand in by_id.items():
        view = quota_view(pools.get(cand.get("pool")), model_id=cand.get("runtime_model_id"), now=now, policy=pol)
        health = _health_block(state, cand.get("pool"), now) or _health_block(state, "profile:" + pid, now)
        reasons = _filters(cand, req, pol, floor, health)
        zone = zone_for(view, pol, req["task_class"])
        if zone == Z_EXHAUSTED:
            reasons.append("QUOTA_EXHAUSTED")
        evals[pid] = {"profile_id": pid, "pool": cand.get("pool"), "runtime_model_id": cand.get("runtime_model_id"),
                      "trust": str(cand.get("trust") or SECONDARY).upper(),
                      "capability_class": int(cand.get("capability_class", 0)),
                      "cost_weight": cand.get("cost_weight", 1.0), "quota": view, "zone": zone,
                      "rejected_by": reasons, "eligible": not reasons}

    pref_class = evals[preferred_id]["capability_class"] if preferred_id in evals else None

    def finish(selected: str | None, reason: str, *, ranked: Sequence[str] = (), detail: str | None = None,
               switch: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rows = []
        for pid, ev in evals.items():
            score = score_candidate(ev, req, pol, floor, preferred_id, pref_class) if ev["eligible"] else None
            rows.append({"profile_id": pid, "pool": ev["pool"], "trust": ev["trust"],
                         "capability_class": ev["capability_class"], "eligible": ev["eligible"],
                         "rejected_by": ev["rejected_by"], "zone": ev["zone"],
                         "quota": {k: ev["quota"][k] for k in ("percent", "certainty", "binding_limit",
                                                                "resets_in_minutes", "blocked", "unknown_windows")},
                         "limits": ev["quota"]["limits"], "score": score})
        chosen = evals.get(selected) if selected else None
        return {"router_version": ROUTER_VERSION, "selected_profile_id": selected,
                "selected_pool": chosen["pool"] if chosen else None,
                "preferred_profile_id": preferred_id, "reason_code": reason, "detail": detail,
                "switched": bool(selected and selected != preferred_id),
                "ranked_profile_ids": list(ranked) if ranked else ([selected] if selected else []),
                "task_class": req["task_class"], "criticality": req["criticality"], "phase": req["phase"],
                "required_capabilities": req["required_capabilities"], "capability_class_floor": floor,
                "selected_quota": ({k: chosen["quota"][k] for k in ("percent", "certainty", "binding_limit",
                                                                    "resets_in_minutes")} if chosen else None),
                "selected_trust": chosen["trust"] if chosen else None,
                "switch_penalty_applied": (score_candidate(chosen, req, pol, floor, preferred_id, pref_class)
                                           ["components"]["switch_penalty"] if chosen else 0.0),
                "alternatives": [r for r in rows if r["profile_id"] != selected],
                "evaluated": rows,
                "thresholds": {k: pol[k] for k in ("soft_threshold_percent", "reserve_percent",
                                                   "near_reset_minutes", "return_threshold_percent")},
                "switch": dict(switch or {}), "router_state": state, "at": _iso(now)}

    # 1. manual override outranks every heuristic, if technically feasible.
    override = req["manual_override"]
    if override:
        cand = by_id.get(override)
        health = (_health_block(state, cand.get("pool"), now) or _health_block(state, "profile:" + override, now)) \
            if cand else None
        infeasible = None
        if cand is None:
            infeasible = "UNKNOWN_PROFILE"
        elif str(cand.get("trust") or "").upper() == DISABLED:
            infeasible = "TRUST_DISABLED"
        elif not cand.get("available", True):
            infeasible = "NOT_AVAILABLE"
        elif health:
            infeasible = f"PROVIDER_BLOCKED:{health['status']}"
        if infeasible is None:
            rest = [pid for pid, ev in evals.items() if ev["eligible"] and pid != override]
            return finish(override, "MANUAL_OVERRIDE", ranked=[override, *rest])
        manual_note = f"MANUAL_OVERRIDE_INFEASIBLE:{infeasible}"
    else:
        manual_note = None

    def done(selected: str | None, reason: str, **kw: Any) -> dict[str, Any]:
        detail = kw.pop("detail", None)
        if manual_note:
            detail = (detail + "; " if detail else "") + manual_note
        return finish(selected, reason, detail=detail, **kw)

    if not pol["enabled"]:
        return done(preferred_id if preferred_id in by_id else None, "ROUTER_DISABLED")
    pref = evals.get(preferred_id) if preferred_id else None
    if preferred_id and pref is None:
        return done(preferred_id, "PREFERRED_NOT_ROUTABLE", detail="preferred profile has no routing traits; unchanged")

    # 2. clear hysteresis entries that have recovered / been reset.
    returned: list[dict[str, Any]] = []
    for pool, entry in list(state["demoted"].items()):
        pool_views = [ev["quota"] for ev in evals.values() if ev["pool"] == pool]
        if not pool_views:
            continue
        why = _return_confirmed(entry, pool_views[0], pol, now)
        if why:
            state["demoted"].pop(pool)
            returned.append({"pool": pool, "reason": why})

    reserve_ok = req["phase"] in pol["reserve_allowed_phases"]

    def usable(ev: Mapping[str, Any]) -> bool:
        return ev["eligible"] and (ev["zone"] != Z_RESERVE or reserve_ok)

    def ranked_alternatives(*, exclude: str | None, only_better_than: Mapping[str, Any] | None) -> list[str]:
        rows = []
        for pid, ev in evals.items():
            if pid == exclude or not usable(ev) or ev["pool"] in state["demoted"]:
                continue
            if only_better_than is not None:
                better = ev["zone"] == Z_NORMAL or (
                    ev["quota"]["percent"] is not None and only_better_than["quota"]["percent"] is not None
                    and ev["quota"]["percent"] > only_better_than["quota"]["percent"]
                    and ev["zone"] not in (Z_RESERVE, Z_LOW))
                if not better:
                    continue
            rows.append((score_candidate(ev, req, pol, floor, preferred_id, pref_class)["total"], pid))
        return [pid for _, pid in sorted(rows, key=lambda r: (-r[0], r[1]))]

    def demote(ev: Mapping[str, Any], reason: str) -> None:
        state["demoted"].setdefault(ev["pool"], {"since": _iso(now), "profile_id": ev["profile_id"],
                                                 "percent": ev["quota"]["percent"],
                                                 "certainty": ev["quota"]["certainty"], "reason": reason})

    def switch_info(frm: Mapping[str, Any] | None, to: str | None, why: str) -> dict[str, Any]:
        return {"from_profile_id": frm["profile_id"] if frm else None, "from_pool": frm["pool"] if frm else None,
                "to_profile_id": to, "to_pool": evals[to]["pool"] if to else None, "why": why,
                "returned_pools": returned}

    # 3. a repo-modifying operation already under way is not migrated for a mere estimate change.
    cur = evals.get(req["current_profile_id"]) if req["current_profile_id"] else None
    if req["operation_in_progress"] and req["repo_modifying"] and cur is not None and cur["eligible"] \
            and cur["zone"] != Z_EXHAUSTED:
        rest = [p for p in ranked_alternatives(exclude=cur["profile_id"], only_better_than=None)]
        return done(cur["profile_id"], "OPERATION_IN_PROGRESS_NO_MIGRATE",
                    ranked=[cur["profile_id"], *rest], switch=switch_info(None, None, "operation in progress"))

    # 4. preferred is not eligible at all → best adequate alternative.
    if pref is None or not pref["eligible"]:
        why = (pref["rejected_by"][0].split(":")[0] if pref and pref["rejected_by"] else "NO_PREFERRED")
        order = ranked_alternatives(exclude=preferred_id, only_better_than=None)
        if not order and pol["reserve_last_resort"]:
            order = [pid for pid, ev in evals.items() if ev["eligible"] and pid != preferred_id]
        if pref is not None and "QUOTA_EXHAUSTED" in pref["rejected_by"]:
            demote(pref, "QUOTA_EXHAUSTED")
        if not order:
            return done(None, "NO_ELIGIBLE_PROFILE", detail=f"preferred rejected: {why}")
        return done(order[0], f"PREFERRED_INELIGIBLE:{why}", ranked=order,
                    switch=switch_info(pref, order[0], why))

    # 5. preferred is eligible: decide by its zone.
    zone = pref["zone"]
    held = pref["pool"] in state["demoted"]       # hysteresis still holding after a quota switch
    effective = Z_LOW if held and zone in (Z_NORMAL, Z_UNKNOWN, Z_LOW) else zone
    others = ranked_alternatives(exclude=preferred_id, only_better_than=None)

    if effective in (Z_NORMAL, Z_UNKNOWN):
        reason = "RETURN_" + returned[0]["reason"] if returned and returned[0]["pool"] == pref["pool"] else \
            ("QUOTA_OK" if zone == Z_NORMAL else "QUOTA_UNKNOWN_NO_CHANGE")
        return done(preferred_id, reason, ranked=[preferred_id, *others], switch=switch_info(None, None, reason))

    if effective == Z_LOW:
        reset = pref["quota"]["resets_in_minutes"]
        small_enough = _task_rank(req["task_class"]) <= _task_rank(pol["near_reset_max_task_class"])
        stays_current = req["current_pool"] in (None, pref["pool"])
        if not held and reset is not None and reset <= pol["near_reset_minutes"] and small_enough and stays_current:
            return done(preferred_id, "NEAR_RESET_STAY", ranked=[preferred_id, *others],
                        switch=switch_info(None, None, "reset imminent; small task stays"))
        better = ranked_alternatives(exclude=preferred_id, only_better_than=pref)
        if better:
            demote(pref, "QUOTA_LOW")
            code = "HYSTERESIS_HOLD" if held else "QUOTA_LOW_ALTERNATIVE"
            return done(better[0], code, ranked=[*better, preferred_id, *[o for o in others if o not in better]],
                        switch=switch_info(pref, better[0], code))
        return done(preferred_id, "QUOTA_LOW_NO_BETTER_ALTERNATIVE", ranked=[preferred_id, *others],
                    switch=switch_info(None, None, "no adequate alternative with more headroom"))

    # effective == Z_RESERVE
    demote(pref, "QUOTA_RESERVE")
    if reserve_ok:
        return done(preferred_id, "RESERVE_PROTECTED_PHASE_ALLOWED", ranked=[preferred_id, *others],
                    switch=switch_info(None, None, "reserve is kept for review/repair phases"))
    if others:
        return done(others[0], "RESERVE_PROTECTED_SWITCH", ranked=others, switch=switch_info(pref, others[0], "reserve"))
    if pol["reserve_last_resort"]:
        return done(preferred_id, "RESERVE_LAST_RESORT", ranked=[preferred_id],
                    switch=switch_info(None, None, "no alternative"))
    return done(None, "NO_ELIGIBLE_PROFILE", detail="RESERVE_PROTECTED: only reserve capacity remains for this phase")
