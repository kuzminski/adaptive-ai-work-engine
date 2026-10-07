"""Cost-per-solved-task statistics shared by AAW-Bench (lab) and the product's passive experience engine.

Pure functions over rows shaped like `{task_id, difficulty, profile_id, solved, cost_usd, proxy_units, wall_s, ...}`.
A row is one attempt of one profile at one task (a lab trial or a real iteration). Nothing here reads files.
"""
from __future__ import annotations

import itertools
import math
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

DIFFICULTIES = ("EASY", "MEDIUM", "HARD")
# difficulty band -> the AAW implementation tier whose profile it informs
TIER_OF = {"EASY": ("NORMAL", "implementer_default"), "MEDIUM": ("HARDER", "implementer_harder"),
           "HARD": ("SIGNIFICANTLY_DIFFICULT", "implementer_hard")}
COMPLEXITY_DIFFICULTY = {"NORMAL": "EASY", "HARDER": "MEDIUM", "SIGNIFICANTLY_DIFFICULT": "HARD"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def choose_basis(rows: Sequence[Mapping[str, Any]], basis: str = "auto") -> str:
    if basis in ("usd", "proxy"):
        return basis
    return "usd" if rows and all(r.get("cost_usd") is not None for r in rows) else "proxy"


def cost_of(row: Mapping[str, Any], basis: str) -> float:
    value = row.get("cost_usd") if basis == "usd" else row.get("proxy_units")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _group(rows: Iterable[Mapping[str, Any]], *keys: str) -> dict[Any, list[Mapping[str, Any]]]:
    out: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        out[tuple(row[k] for k in keys) if len(keys) > 1 else row[keys[0]]].append(row)
    return out


def stat_block(rows: Sequence[Mapping[str, Any]], basis: str) -> dict[str, Any]:
    n, solved = len(rows), sum(1 for r in rows if r["solved"])
    total = sum(cost_of(r, basis) for r in rows)
    lo, hi = wilson(solved, n)
    walls = [r["wall_s"] for r in rows if isinstance(r.get("wall_s"), (int, float))]
    return {"n": n, "solved": solved, "rate": round(solved / n, 4) if n else None, "rate_ci95": [lo, hi],
            "cost_mean": round(total / n, 4) if n else None,
            "cost_per_solved": round(total / solved, 4) if solved else None,
            "wall_mean_s": round(sum(walls) / len(walls), 1) if walls else None}


def profile_stats(rows: Sequence[Mapping[str, Any]], basis: str) -> dict[str, dict[str, Any]]:
    out = {}
    for profile, group in _group(rows, "profile_id").items():
        block = stat_block(group, basis)
        block["by_difficulty"] = {d: stat_block(g, basis) for d, g in sorted(_group(group, "difficulty").items())}
        out[profile] = block
    return out


def pareto(stats: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Profiles not beaten on both cost per solved task (lower) and solve rate (higher)."""
    live = {p: s for p, s in stats.items() if s["cost_per_solved"] is not None}
    front = []
    for p, s in live.items():
        dominated = any(o is not s and o["cost_per_solved"] <= s["cost_per_solved"] and o["rate"] >= s["rate"]
                        and (o["cost_per_solved"] < s["cost_per_solved"] or o["rate"] > s["rate"]) for o in live.values())
        if not dominated:
            front.append(p)
    return sorted(front, key=lambda p: live[p]["cost_per_solved"])


def cascade(rows: Sequence[Mapping[str, Any]], ladder: Sequence[str], basis: str) -> dict[str, Any] | None:
    """Expected result of trying `ladder` in order, stopping at the first success.

    Per task, with p_i the measured solve rate and c_i the mean cost of profile i on that task and attempts
    independent: cost = sum_i P(all earlier fail) * c_i, success = 1 - prod(1 - p_i). Tasks without data for
    every rung of the ladder make the ladder unevaluable (None) - no guessing.
    """
    by_task = _group(rows, "task_id")
    per_task = []
    for task_id, group in by_task.items():
        cells = _group(group, "profile_id")
        if any(p not in cells for p in ladder):
            return None
        fail_before, expected, difficulty = 1.0, 0.0, group[0]["difficulty"]
        for profile in ladder:
            cell = cells[profile]
            p = sum(1 for r in cell if r["solved"]) / len(cell)
            expected += fail_before * (sum(cost_of(r, basis) for r in cell) / len(cell))
            fail_before *= 1 - p
        per_task.append({"task_id": task_id, "difficulty": difficulty, "cost": expected, "success": 1 - fail_before})
    if not per_task:
        return None
    success = sum(t["success"] for t in per_task) / len(per_task)
    cost = sum(t["cost"] for t in per_task) / len(per_task)
    return {"ladder": list(ladder), "tasks": len(per_task), "success_rate": round(success, 4),
            "cost_per_task": round(cost, 4), "cost_per_solved": round(cost / success, 4) if success else None}


def best_ladders(rows: Sequence[Mapping[str, Any]], basis: str, *, max_len: int = 3, floor: float = 0.9,
                 top: int = 5) -> list[dict[str, Any]]:
    profiles = sorted({r["profile_id"] for r in rows})
    found = []
    for size in range(1, max_len + 1):
        for ladder in itertools.permutations(profiles, size):
            result = cascade(rows, ladder, basis)
            if result and result["success_rate"] >= floor:
                found.append(result)
    found.sort(key=lambda r: (r["cost_per_task"], len(r["ladder"])))
    return found[:top]


def tier_recommendations(rows: Sequence[Mapping[str, Any]], basis: str, *, floor: float, min_n: int) -> dict[str, Any]:
    """Per difficulty band: the cheapest profile (cost per solved task) whose solve rate clears the floor."""
    out: dict[str, Any] = {}
    for difficulty in DIFFICULTIES:
        tier, slot = TIER_OF[difficulty]
        band = [r for r in rows if r["difficulty"] == difficulty]
        candidates = []
        for profile, group in _group(band, "profile_id").items():
            block = stat_block(group, basis)
            if block["n"] < min_n:
                continue
            if block["rate"] >= floor and block["cost_per_solved"] is not None:
                candidates.append((block["cost_per_solved"], -block["rate"], profile, block))
        if candidates:
            candidates.sort(key=lambda c: c[:3])
            _, _, profile, block = candidates[0]
            out[difficulty] = {"tier": tier, "slot": slot, "profile_id": profile, "status": "RECOMMENDED", **block}
        else:
            seen = {p for p, g in _group(band, "profile_id").items() if len(g) >= min_n}
            out[difficulty] = {"tier": tier, "slot": slot, "profile_id": None,
                               "status": "NO_PROFILE_MEETS_FLOOR" if seen else "INSUFFICIENT_DATA",
                               "detail": f"needs >= {min_n} trials per profile and a solve rate >= {floor}"}
    return out
