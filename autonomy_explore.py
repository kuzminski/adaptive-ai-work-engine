"""AAW CONTROLLED EXPLORATION V1 - new models get data from real work, inside a visible budget.

Opt-in. When enabled, a small, deterministic share of *ordinary* iterations is implemented by a candidate profile
instead of the policy's default, so that cells of the user's own benchmark that have too few trials fill up without a
lab run. Pure decision logic (no I/O, no randomness); the controller applies it and records every choice.

Hard rules - exploration never happens when any of them fails:
  * only the default implementer slot of an ordinary iteration (tier NORMAL, no human override, no frozen-charter
    risk floor); never repairs, reviews, planning, harder tiers or `critical_scope` runs;
  * never the first iteration (it carries the human's own acceptance criteria);
  * only candidates the user's setup already runs and that cost no more than the default implementer
    (the product builds that list; a candidate that cannot run *now* is skipped, never substituted into an error);
  * a reviewer must still be a different model when the run demands independence;
  * a budget: at most `max_percent` of eligible iterations and `max_per_run` explored iterations per run;
  * only for task kinds whose benchmark cell for that candidate is still thin (`wanted`).
Everything is journaled (`EXPLORATION_SELECTED`) and the iteration's selection reason is `EXPLORATION`, so the
experience view can mark and count exploration trials.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "AAW_EXPLORATION_V1"
REASON = "EXPLORATION"
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "max_percent": 20,          # of eligible iterations
    "max_per_run": 3,
    "candidates": [],           # profile IDs, cost class <= the default implementer's, runnable on this machine
    "wanted": {},               # task kind -> candidate IDs whose cell is still thin, thinnest first
    "exclude_kinds": ["INFRA"],
    "first_explored_eligible": 2,
}


class ExplorationConfigError(ValueError):
    pass


def normalize_config(raw: Any, *, known_profiles: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if raw is None or raw is False:
        return {**DEFAULTS, "candidates": [], "wanted": {}, "exclude_kinds": list(DEFAULTS["exclude_kinds"]), "enabled": False}
    if not isinstance(raw, Mapping):
        raise ExplorationConfigError("exploration must be an object")
    unknown = sorted(set(raw) - set(DEFAULTS) - {"_comment", "schema"})
    if unknown:
        raise ExplorationConfigError(f"unknown exploration keys: {unknown}")
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k not in ("_comment", "schema")}}
    if type(cfg["enabled"]) is not bool:
        raise ExplorationConfigError("exploration.enabled must be a boolean")
    if type(cfg["max_percent"]) is not int or not 1 <= cfg["max_percent"] <= 50:
        raise ExplorationConfigError("exploration.max_percent must be an integer in 1..50")
    if type(cfg["max_per_run"]) is not int or not 1 <= cfg["max_per_run"] <= 10:
        raise ExplorationConfigError("exploration.max_per_run must be an integer in 1..10")
    if type(cfg["first_explored_eligible"]) is not int or not 2 <= cfg["first_explored_eligible"] <= 20:
        raise ExplorationConfigError("exploration.first_explored_eligible must be an integer in 2..20")
    candidates = cfg["candidates"]
    if not (isinstance(candidates, list) and all(isinstance(c, str) and c.strip() for c in candidates)) or len(candidates) > 12:
        raise ExplorationConfigError("exploration.candidates must be a list of up to 12 profile IDs")
    if known_profiles is not None:
        missing = [c for c in candidates if c not in known_profiles]
        if missing:
            raise ExplorationConfigError(f"exploration.candidates name unknown profiles: {missing}")
    wanted = cfg["wanted"]
    if not (isinstance(wanted, Mapping) and all(isinstance(k, str) and isinstance(v, list) and all(isinstance(p, str) for p in v)
                                                for k, v in wanted.items())):
        raise ExplorationConfigError("exploration.wanted must map task kinds to profile ID lists")
    stray = sorted({p for v in wanted.values() for p in v} - set(candidates))
    if stray:
        raise ExplorationConfigError(f"exploration.wanted names profiles that are not candidates: {stray}")
    if not (isinstance(cfg["exclude_kinds"], list) and all(isinstance(k, str) for k in cfg["exclude_kinds"])):
        raise ExplorationConfigError("exploration.exclude_kinds must be a list of task kinds")
    return {**cfg, "candidates": list(candidates), "wanted": {k: list(v) for k, v in wanted.items()},
            "exclude_kinds": list(cfg["exclude_kinds"])}


def period(cfg: Mapping[str, Any]) -> int:
    """Explore every `period`-th eligible iteration (20% -> every 5th)."""
    return max(1, math.ceil(100 / int(cfg["max_percent"])))


def empty_state() -> dict[str, Any]:
    return {"eligible": 0, "explored": 0, "decisions": {}}


def decide(cfg: Mapping[str, Any], counters: Mapping[str, Any], *, kind: str, tier: str, reason: str,
           iteration_index: int, critical: bool, candidate_ok: Callable[[str], bool]) -> dict[str, Any]:
    """Return {explore, profile_id, code, eligible_after}. `counters` is not mutated; the caller records the outcome."""
    eligible = int(counters.get("eligible", 0))

    def out(explore: bool, code: str, profile: str | None = None, counted: bool = False) -> dict[str, Any]:
        return {"explore": explore, "profile_id": profile, "code": code, "eligible_after": eligible + (1 if counted else 0)}

    if not cfg["enabled"]:
        return out(False, "DISABLED")
    if tier != "NORMAL" or reason != "DEFAULT_IMPLEMENTATION":
        return out(False, "NOT_ORDINARY_IMPLEMENTATION")
    if critical:
        return out(False, "CRITICAL_SCOPE")
    if iteration_index < 2:
        return out(False, "FIRST_ITERATION")
    if kind in cfg["exclude_kinds"]:
        return out(False, "KIND_EXCLUDED")
    wanted = [p for p in cfg["wanted"].get(kind, []) if p in cfg["candidates"]]
    if not wanted:
        return out(False, "NO_THIN_CELL")
    if int(counters.get("explored", 0)) >= int(cfg["max_per_run"]):
        return out(False, "BUDGET_EXHAUSTED", counted=True)
    seen = eligible + 1
    first = int(cfg["first_explored_eligible"])
    if seen < first or (seen - first) % period(cfg) != 0:
        return out(False, "NOT_THIS_TURN", counted=True)
    for profile in wanted:
        if candidate_ok(profile):
            return out(True, REASON, profile, counted=True)
    return out(False, "NO_RUNNABLE_CANDIDATE", counted=True)


def selection(profile_id: str, *, default_profile_id: str, kind: str, policy_version: str) -> dict[str, Any]:
    """A selection record shaped like the policy's own (see autonomy_policy._selection)."""
    return {"policy_version": policy_version, "profile_key": "explore:" + profile_id, "profile_id": profile_id,
            "selection_reason": REASON, "tier": "EXPLORE", "complexity_risk_evidence": [f"EXPLORATION:{kind}"],
            "previous_attempt": None, "escalated_from": default_profile_id}
