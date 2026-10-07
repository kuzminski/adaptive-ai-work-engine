"""AAW EXPERIENCE V1 - the benchmark that builds itself from real work.

Nothing here is a workflow the user must run. Every product run already writes raw evidence (the engine state
and `telemetry.jsonl`). This module *derives* everything else from it, on demand and read-only:

  * `classify_task`   - what kind of work an iteration was (bug fix, feature, refactor, ...), deterministically;
  * `rows_from_state` - one row per real iteration: profile, outcome, implementation cost, total cost;
  * `benchmark`       - cost per solved task per profile **per task kind**, with intervals, Pareto front, optimum;
  * `recommendations` - plain-language "what would have been optimal", only where the data supports it;
  * `horizon`         - how far runs like this actually get before a human is needed;
  * `forecast`        - an honest range for the rest of a running run (or nothing, when there is no data);
  * `live_view`       - the run as a journey: stage strip, chain slots, cost meter, event feed.

The index is disposable (rebuilt from raw files, cached by mtime). Two people with different work get
different benchmarks because the benchmark is *per task kind* and the overall optimum is weighted by the
user's own mix of kinds. Every claim carries its sample size; below the thresholds it says "too little data"
instead of guessing.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import aaw_benchstats as stats
import aaw_telemetry as tel

SCHEMA = "AAW_EXPERIENCE_V1"
ROW_SCHEMA = "AAW_EXPERIENCE_ROW_V1"
MIN_N = 3                 # trials of a profile in a cell before it may be compared
GOOD_N = 8                # ... before the comparison is called reliable
SOLVE_FLOOR = 0.8         # solve rate a profile must reach to be recommended
SWITCH_MARGIN = 0.10      # a cheaper profile must save at least 10% to be worth a switch
MIN_RUNS_HORIZON = 3

KINDS = ("BUGFIX", "FEATURE", "REFACTOR", "TESTS", "DOCS", "UI", "INFRA", "ANALYSIS", "OTHER")
KIND_LABEL = {"BUGFIX": "Błędy", "FEATURE": "Nowe funkcje", "REFACTOR": "Refaktoryzacja", "TESTS": "Testy",
              "DOCS": "Dokumentacja", "UI": "Interfejs", "INFRA": "Build i konfiguracja", "ANALYSIS": "Analiza i badanie",
              "OTHER": "Inne"}

# -- task kind ------------------------------------------------------------------------------

_KEYWORDS: dict[str, tuple[tuple[str, float], ...]] = {
    "BUGFIX": ((r"\bbug", 2), (r"b[łl]ęd", 2), (r"\bfix", 2), (r"napraw", 2), (r"regres", 2), (r"crash", 2),
               (r"nie dzia[łl]a", 2), (r"hotfix", 2), (r"defekt", 2), (r"popraw", 1), (r"zepsut", 2), (r"wyjątk", 1)),
    "REFACTOR": ((r"refaktor", 3), (r"refactor", 3), (r"uporządkuj", 2), (r"wydziel", 2), (r"\bextract", 2),
                 (r"\brename", 2), (r"clean ?up", 2), (r"uprość", 2), (r"simplif", 2), (r"deduplic", 2), (r"przenieś", 1)),
    "TESTS": ((r"\btest", 1.5), (r"pokrycie", 2), (r"coverage", 2), (r"pytest", 2), (r"jednostkow", 1.5)),
    "DOCS": ((r"dokument", 2), (r"readme", 3), (r"\bdocs?\b", 2), (r"changelog", 2), (r"komentarz", 1.5),
             (r"instrukcj", 2), (r"opis ", 1)),
    "UI": ((r"\bui\b", 2), (r"\bux\b", 2), (r"interfejs", 2), (r"widok", 1.5), (r"\bcss\b", 2), (r"\bhtml\b", 2),
           (r"front", 1.5), (r"ekran", 1.5), (r"przycisk", 2), (r"button", 2), (r"layout", 2), (r"styl", 1.5),
           (r"strona", 1), (r"landing", 2)),
    "INFRA": ((r"\bci\b", 2), (r"pipeline", 2), (r"workflow", 1), (r"docker", 3), (r"\bbuild", 1.5), (r"deploy", 2),
              (r"konfigur", 1.5), (r"\bconfig", 1.5), (r"release", 2), (r"paczk", 1.5), (r"packag", 2),
              (r"github actions", 3), (r"instalat", 2), (r"zależno", 1.5), (r"dependenc", 1.5)),
    "ANALYSIS": ((r"analiz", 2), (r"zbadaj", 2), (r"investigat", 2), (r"research", 2), (r"porównaj", 2),
                 (r"\baudyt", 2), (r"audit", 2), (r"raport", 1.5), (r"sprawdź", 1), (r"ocen[aię]", 1), (r"zbadan", 1)),
    "FEATURE": ((r"\bdodaj", 1.5), (r"\badd\b", 1.5), (r"implement", 1.5), (r"zaimplementuj", 2), (r"nowa funkcj", 2),
                (r"\bfeature", 2), (r"stwórz", 1.5), (r"utwórz", 1.5), (r"\bcreate", 1.5), (r"obsług", 1.5),
                (r"\bsupport", 1), (r"wprowadź", 1.5)),
}
_PATH_HINTS: tuple[tuple[str, str, float], ...] = (
    (r"(^|/)tests?(/|$)|test_[^/]*\.py$|\.test\.[jt]sx?$", "TESTS", 1.5),
    (r"(^|/)docs?(/|$)|\.md$|\.rst$", "DOCS", 1.5),
    (r"\.(css|scss|html|vue|svelte|tsx|jsx)$|(^|/)(ui|frontend|static|public)(/|$)", "UI", 1.5),
    (r"(^|/)\.github(/|$)|dockerfile|\.ya?ml$|(^|/)(packaging|build|ci)(/|$)|requirements|pyproject|package\.json", "INFRA", 1.5),
)
_PRIORITY = ("BUGFIX", "REFACTOR", "TESTS", "DOCS", "UI", "INFRA", "ANALYSIS", "FEATURE")


def classify_task(goal: str | None, *, touched: Sequence[str] = (), criteria: Sequence[str] = ()) -> dict[str, Any]:
    """Deterministic task kind from the iteration's goal (strong), acceptance criteria (weak) and touched paths."""
    scores: dict[str, float] = defaultdict(float)
    goal_text = (goal or "").lower()
    for kind, rules in _KEYWORDS.items():
        for pattern, weight in rules:
            if re.search(pattern, goal_text):
                scores[kind] += weight
            if any(re.search(pattern, str(c).lower()) for c in criteria):
                scores[kind] += weight * 0.25
    for path in touched:
        norm = str(path).replace("\\", "/").lower()
        for pattern, kind, weight in _PATH_HINTS:
            if re.search(pattern, norm):
                scores[kind] += weight / max(1, len(touched)) * min(len(touched), 3)
    if not scores:
        return {"kind": "OTHER", "confidence": 0.0, "scores": {}}
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], _PRIORITY.index(kv[0])))
    top, second = ranked[0][1], ranked[1][1] if len(ranked) > 1 else 0.0
    return {"kind": ranked[0][0], "confidence": round((top - second) / top, 2) if top else 0.0,
            "scores": {k: round(v, 2) for k, v in ranked}}


# -- rows -----------------------------------------------------------------------------------

def _priced(records: Sequence[Mapping[str, Any]]) -> float | None:
    values = [r.get("cost_usd") for r in records]
    if not values or any(v is None for v in values):
        return None
    return round(sum(values), 6)


def _units(records: Sequence[Mapping[str, Any]]) -> float:
    return round(sum(r.get("proxy_units") or 0 for r in records), 1)


def _wall(records: Sequence[Mapping[str, Any]]) -> float:
    return round(sum(r.get("wall_s") or 0 for r in records), 1)


def run_finished(state: Mapping[str, Any]) -> bool:
    return state.get("status") != "RUNNING"


def rows_from_state(state: Mapping[str, Any], records: Sequence[Mapping[str, Any]], *,
                    project: str | None = None) -> list[dict[str, Any]]:
    """One row per real iteration of a run.

    `solved` is True (accepted), False (escalated or abandoned in a finished run) or None (still open, or provisional
    work whose chain was never closed - neither success nor failure). Implementation cost is execute + repair +
    diagnose; total cost adds planning, verification and review, with a chain-closing review shared equally by the
    iterations of its chain.
    """
    run_id = str(state.get("run_id"))
    iterations = state.get("iterations") or []
    finished = run_finished(state)
    by_chain: dict[Any, list[str]] = defaultdict(list)
    for it in iterations:
        by_chain[(it.get("chain") or {}).get("chain_id")].append(it["iteration_id"])
    owned: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for rec in records:
        if rec.get("review_mode") == "CHAIN_CLOSE" and rec.get("category") == "REVIEW" and rec.get("chain_id") is not None:
            members = by_chain.get(rec["chain_id"]) or [rec.get("iteration_id")]
            share = {**rec, "cost_usd": None if rec.get("cost_usd") is None else rec["cost_usd"] / len(members),
                     "proxy_units": (rec.get("proxy_units") or 0) / len(members),
                     "wall_s": (rec.get("wall_s") or 0) / len(members)}
            for member in members:
                owned[member].append(share)
        else:
            owned[rec.get("iteration_id")].append(rec)
    rows = []
    for it in iterations:
        mine = owned.get(it["iteration_id"], [])
        impl = [r for r in mine if r.get("category") == "IMPLEMENTATION"]
        first = next((r for r in impl if r.get("executor") == "execute"), None)
        plan = it.get("plan") or {}
        kind = classify_task(plan.get("goal"), touched=plan.get("touched_areas") or [],
                             criteria=plan.get("acceptance_criteria") or [])
        status = it.get("status")
        solved = True if status == "ACCEPTED" else False if status == "ESCALATED" or (
            status == "IN_PROGRESS" and finished) else None
        chain = it.get("chain") or {}
        rows.append({
            "schema": ROW_SCHEMA, "run_id": run_id, "project": project, "iteration_id": it["iteration_id"],
            "index": it.get("index"), "kind": kind["kind"], "kind_confidence": kind["confidence"],
            "difficulty": stats.COMPLEXITY_DIFFICULTY.get(str(plan.get("implementation_complexity") or "NORMAL"), "EASY"),
            "task_id": f"{run_id}:{it['iteration_id']}", "profile_id": (first or {}).get("profile_id"),
            "model": (first or {}).get("model"), "effort": (first or {}).get("effort"),
            "solved": solved, "status": status, "repairs": it.get("repair_attempts", 0),
            "explored": (first or {}).get("selection_reason") == "EXPLORATION",
            "chain_id": chain.get("chain_id"), "review_mode": chain.get("review_mode"),
            "cost_usd": _priced(impl), "proxy_units": _units(impl), "wall_s": _wall(impl),
            "total_cost_usd": _priced(mine), "total_proxy_units": _units(mine), "total_wall_s": _wall(mine),
            "calls": len(mine), "started_at": it.get("started_at"), "finished_at": it.get("finished_at"),
            "source": "PRODUCTION"})
    return [r for r in rows if r["profile_id"]]


def scoped(rows: Sequence[Mapping[str, Any]], scope: str) -> list[dict[str, Any]]:
    """Rows as `aaw_benchstats` reads them: `scope='total'` swaps in the all-in cost of the iteration."""
    if scope == "total":
        return [{**r, "cost_usd": r.get("total_cost_usd"), "proxy_units": r.get("total_proxy_units"),
                 "wall_s": r.get("total_wall_s")} for r in rows]
    return [dict(r) for r in rows]


OUTCOMES = ("FINISHED", "CAP", "ESCALATED", "RUNNING")


def run_summary(state: Mapping[str, Any], records: Sequence[Mapping[str, Any]], task: Mapping[str, Any] | None = None,
                rows: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    hold, escalation = state.get("hold") or {}, state.get("escalation")
    if state.get("status") == "RUNNING":
        outcome, why = "RUNNING", None
    elif escalation:
        outcome, why = "ESCALATED", escalation.get("code")
    elif hold.get("reason") == "ITERATION_CAP_REACHED":
        outcome, why = "CAP", hold.get("reason")
    else:
        outcome, why = "FINISHED", hold.get("reason")
    iterations = state.get("iterations") or []
    accepted = sum(1 for i in iterations if i.get("status") == "ACCEPTED")
    form = (task or {}).get("form") or {}
    return {"run_id": state.get("run_id"), "goal": form.get("goal") or state["mandate"]["iteration_contract"].get("goal"),
            "project": ((task or {}).get("workspace") or {}).get("project_name"),
            "outcome": outcome, "why": why, "iterations": len(iterations), "accepted": accepted,
            "repairs": sum(i.get("repair_attempts", 0) for i in iterations),
            "chains_closed": (state.get("chain_state") or {}).get("closed_chains", 0),
            "deferred_open": sum(1 for f in state.get("deferred_findings", []) if f.get("status") == "OPEN"),
            "kinds": dict(Counter(r["kind"] for r in rows)), "calls": len(records),
            "cost_usd": _priced(records), "proxy_units": _units(records), "wall_s": _wall(records),
            "started_at": state.get("started_at"), "updated_at": state.get("updated_at")}


# -- the passive store ------------------------------------------------------------------------

_CACHE: dict[str, tuple[tuple, dict[str, Any]]] = {}


def _stamp(*paths: Path) -> tuple:
    out = []
    for path in paths:
        try:
            st = path.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def scan_stamp(runs_root: Path) -> tuple:
    """A cheap fingerprint of everything `scan` would read (file stat only): equal stamps = equal scan result."""
    stamps = []
    if Path(runs_root).is_dir():
        for run_dir in sorted(Path(runs_root).iterdir()):
            adir = run_dir / "AUTONOMY"
            if (adir / "autonomy_state.json").is_file():
                stamps.append((run_dir.name, _stamp(adir / "autonomy_state.json", adir / "telemetry.jsonl",
                                                    run_dir / "PRODUCT" / "task.json")))
    return tuple(stamps)


def load_run(run_dir: Path, *, pricing: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Rows, records and summary of one run directory (`<runs_root>/<run_id>`); cached by file mtime."""
    adir = run_dir / "AUTONOMY"
    state_path, tel_path = adir / "autonomy_state.json", adir / "telemetry.jsonl"
    task_path = run_dir / "PRODUCT" / "task.json"
    stamp = _stamp(state_path, tel_path, task_path)
    cached = _CACHE.get(str(run_dir))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        task = {}
    pricing = pricing if pricing is not None else tel.load_pricing()
    records = tel.read_records(tel_path) or tel.reconstruct_from_state(state, adir / "RESULTS", pricing)
    project = ((task.get("workspace") or {}).get("project_name"))
    rows = rows_from_state(state, records, project=project)
    loaded = {"state": state, "task": task, "records": records, "rows": rows,
              "summary": run_summary(state, records, task, rows)}
    _CACHE[str(run_dir)] = (stamp, loaded)
    return loaded


def scan(runs_root: Path, *, pricing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Everything the user's runs say, in one pass. Broken runs are skipped, never fatal."""
    runs = []
    if Path(runs_root).is_dir():
        for run_dir in sorted(Path(runs_root).iterdir()):
            if (run_dir / "AUTONOMY" / "autonomy_state.json").is_file():
                try:
                    loaded = load_run(run_dir, pricing=pricing)
                except Exception:
                    loaded = None
                if loaded:
                    runs.append(loaded)
    return {"runs": runs, "rows": [r for run in runs for r in run["rows"]],
            "records": [r for run in runs for r in run["records"]], "summaries": [run["summary"] for run in runs]}


# -- benchmark --------------------------------------------------------------------------------

def _label(profile_id: str, names: Mapping[str, str] | None) -> str:
    return (names or {}).get(profile_id) or profile_id


CONFIDENCE_PL = {"RELIABLE": "wiarygodna", "PRELIMINARY": "wstępna", "TOO_FEW": "za mała"}


def _confidence(n: int) -> str:
    return "RELIABLE" if n >= GOOD_N else "PRELIMINARY" if n >= MIN_N else "TOO_FEW"


def _points(rows: Sequence[Mapping[str, Any]], basis: str, names: Mapping[str, str] | None) -> list[dict[str, Any]]:
    out = []
    for profile, group in stats._group(rows, "profile_id").items():
        block = stats.stat_block(group, basis)
        out.append({"profile_id": profile, "label": _label(profile, names), **block, "confidence": _confidence(block["n"]),
                    "explored_n": sum(1 for r in group if r.get("explored")),
                    "model": next((r.get("model") for r in group if r.get("model")), None)})
    return out


def best_of(points: Sequence[Mapping[str, Any]], floor: float = SOLVE_FLOOR) -> Mapping[str, Any] | None:
    eligible = [p for p in points if p["n"] >= MIN_N and p["rate"] is not None and p["rate"] >= floor
                and p["cost_per_solved"] is not None]
    return min(eligible, key=lambda p: (p["cost_per_solved"], -p["rate"])) if eligible else None


def mix_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """The user's own work profile: share of each task kind among decided iterations."""
    counts = Counter(r["kind"] for r in rows if r.get("solved") is not None)
    total = sum(counts.values())
    return {k: round(counts[k] / total, 4) for k in KINDS if counts.get(k)} if total else {}


def benchmark(rows: Sequence[Mapping[str, Any]], *, scope: str = "implementation", basis: str = "auto",
              names: Mapping[str, str] | None = None, mix: Mapping[str, float] | None = None) -> dict[str, Any]:
    """Cost per solved task per profile, per task kind - plus an overall view weighted by the user's mix."""
    decided = [r for r in scoped(rows, scope) if r.get("solved") is not None and r.get("profile_id")]
    basis = stats.choose_basis(decided, basis)
    mix = dict(mix) if mix is not None else mix_of(decided)
    kinds: dict[str, Any] = {}
    for kind in KINDS:
        group = [r for r in decided if r["kind"] == kind]
        if not group:
            continue
        points = _points(group, basis, names)
        # only profiles with enough trials can dominate or be dominated: two lucky runs must not outrank a measured model
        front = stats.pareto({p["profile_id"]: p for p in points if p["n"] >= MIN_N})
        best = best_of(points)
        for p in points:
            p["on_pareto"] = p["profile_id"] in front
            p["is_best"] = bool(best and p["profile_id"] == best["profile_id"])
        kinds[kind] = {"kind": kind, "label": KIND_LABEL[kind], "n": len(group), "points": points, "pareto": front,
                       "best": best["profile_id"] if best else None,
                       "confidence": _confidence(max((p["n"] for p in points), default=0))}
    overall = _overall(kinds, mix, names)
    return {"schema": SCHEMA, "scope": scope, "basis": basis, "unit": "USD" if basis == "usd" else "proxy",
            "rows": len(decided), "mix": mix, "kinds": kinds, "overall": overall,
            "warnings": _warnings(basis, decided)}


def _warnings(basis: str, decided: Sequence[Mapping[str, Any]]) -> list[str]:
    out = []
    if basis == "usd":
        out.append("Ceny to niezweryfikowane ceny katalogowe API; abonament rozlicza limit, nie tokeny.")
    elif len({r.get("model") for r in decided if r.get("model")}) > 1:
        out.append("Brak cen dla części wywołań: porównanie w jednostkach przybliżonych "
                   "(nie uwzględnia różnic cen między rodzinami modeli).")
    return out


def _overall(kinds: Mapping[str, Any], mix: Mapping[str, float], names: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """Per profile: expected cost per solved task under the user's mix, over the kinds where it has enough data."""
    profiles = {p["profile_id"] for k in kinds.values() for p in k["points"]}
    out = []
    for profile in sorted(profiles):
        covered, weighted, rate = 0.0, 0.0, 0.0
        for kind, share in mix.items():
            point = next((p for p in (kinds.get(kind) or {}).get("points", []) if p["profile_id"] == profile), None)
            if point and point["n"] >= MIN_N and point["cost_per_solved"] is not None:
                covered += share
                weighted += share * point["cost_per_solved"]
                rate += share * point["rate"]
        if covered >= 0.5:
            out.append({"profile_id": profile, "label": _label(profile, names), "coverage": round(covered, 2),
                        "cost_per_solved": round(weighted / covered, 6), "rate": round(rate / covered, 4)})
    out.sort(key=lambda r: r["cost_per_solved"])
    # the overall best is the cheapest profile that is also reliable under this mix, not simply the cheapest
    best = next((r for r in out if r["rate"] >= SOLVE_FLOOR), None)
    for r in out:
        r["is_best"] = r is best
    return out


# -- what would be optimal -------------------------------------------------------------------

def cost_structure(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary = tel.summarize(records)
    return {"share_of_proxy_units": summary["share_of_proxy_units"], "calls": summary["records"],
            "by_category": {k: {"calls": v["calls"], "proxy_units": v["proxy_units"], "cost_usd": v["cost_usd"]}
                            for k, v in summary["by_category"].items()}}


def chain_health(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How the serious review is behaving: how often a chain close needed repair (drives the chain length advice)."""
    closes = [r for r in rows if r.get("review_mode") == "CHAIN_CLOSE" and r.get("solved") is not None]
    repaired = sum(1 for r in closes if r.get("repairs"))
    return {"closes": len(closes), "repaired": repaired,
            "repair_rate": round(repaired / len(closes), 2) if closes else None}


def recommendations(bench: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                    records: Sequence[Mapping[str, Any]] = (), summaries: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Plain-language 'what would be optimal', each with its evidence. Silent where the data does not support a claim."""
    out: list[dict[str, Any]] = []
    decided = [r for r in rows if r.get("solved") is not None]
    for kind, block in bench["kinds"].items():
        points = {p["profile_id"]: p for p in block["points"]}
        usage = Counter(r["profile_id"] for r in decided if r["kind"] == kind)
        incumbent = points.get(usage.most_common(1)[0][0]) if usage else None
        best = points.get(block["best"]) if block["best"] else None
        if not best or not incumbent:
            if incumbent and incumbent["n"] < MIN_N:
                out.append({"type": "COLLECT", "kind": kind, "label": block["label"], "profile_id": incumbent["profile_id"],
                            "text": f"{block['label']}: {incumbent['n']} z {MIN_N} prób potrzebnych do porównania modeli — "
                                    "wynik pojawi się sam, w miarę kolejnych zadań.",
                            "evidence": {"n": incumbent["n"]}})
            continue
        if best["profile_id"] == incumbent["profile_id"]:
            out.append({"type": "KEEP", "kind": kind, "label": block["label"], "profile_id": best["profile_id"],
                        "text": f"{block['label']}: {best['label']} jest dziś najlepszym wyborem "
                                f"({best['rate'] * 100:.0f}% skuteczności, {CONFIDENCE_PL[best['confidence']]} próba, n={best['n']}).",
                        "evidence": {"n": best["n"], "rate": best["rate"], "cost_per_solved": best["cost_per_solved"]}})
            continue
        cheaper = (incumbent["cost_per_solved"] is None or
                   best["cost_per_solved"] <= (incumbent["cost_per_solved"] or math.inf) * (1 - SWITCH_MARGIN))
        if cheaper and best["rate"] >= (incumbent["rate"] or 0) - 0.05:
            saving = None if incumbent["cost_per_solved"] is None else 1 - best["cost_per_solved"] / incumbent["cost_per_solved"]
            out.append({"type": "SWITCH", "kind": kind, "label": block["label"], "from": incumbent["profile_id"],
                        "to": best["profile_id"], "saving": saving,
                        "text": f"{block['label']}: {best['label']} wypada taniej niż {incumbent['label']}"
                                + (f" (o {saving * 100:.0f}% mniej za rozwiązane zadanie)" if saving is not None else "")
                                + f" przy porównywalnej skuteczności ({best['rate'] * 100:.0f}% vs {(incumbent['rate'] or 0) * 100:.0f}%).",
                        "evidence": {"n_best": best["n"], "n_incumbent": incumbent["n"],
                                     "confidence": min(best["confidence"], incumbent["confidence"], key=_confidence_rank)}})
    structure = cost_structure(records) if records else None
    if structure and structure["calls"] >= 10:
        review = structure["share_of_proxy_units"].get("REVIEW") or 0
        if review >= 0.5:
            out.append({"type": "REVIEW_SHARE", "text": f"{review * 100:.0f}% kosztu to review. Dłuższe łańcuchy "
                        "(mniej review) obniżą rachunek najmocniej — bardziej niż wybór modelu implementacyjnego.",
                        "evidence": structure["share_of_proxy_units"]})
    health = chain_health(rows)
    if health["closes"] >= MIN_N:
        if health["repair_rate"] == 0:
            out.append({"type": "LONGER_CHAINS", "text": f"Review serii nie wymagał napraw w {health['closes']} z "
                        f"{health['closes']} przypadków — łańcuchy można wydłużyć.", "evidence": health})
        elif health["repair_rate"] >= 0.5:
            out.append({"type": "SHORTER_CHAINS", "text": f"Review serii wymagał napraw w {health['repaired']} z "
                        f"{health['closes']} przypadków — krótsze łańcuchy wyłapią błędy wcześniej.", "evidence": health})
    return out


def _confidence_rank(label: str) -> int:
    return {"TOO_FEW": 0, "PRELIMINARY": 1, "RELIABLE": 2}.get(label, 0)


def settlement(run_rows: Sequence[Mapping[str, Any]], bench: Mapping[str, Any]) -> dict[str, Any]:
    """What this run cost and, where the benchmark supports it, what the better choice would have saved (a range)."""
    actual = sum(r.get("cost_usd") or 0 for r in run_rows) if bench["basis"] == "usd" else sum(
        r.get("proxy_units") or 0 for r in run_rows)
    saving_low = saving_high = 0.0
    items = []
    for kind in {r["kind"] for r in run_rows}:
        block = bench["kinds"].get(kind)
        best = next((p for p in (block or {}).get("points", []) if p["is_best"]), None)
        if not block or not best:
            continue
        spent = {}
        for r in run_rows:
            if r["kind"] == kind:
                spent[r["profile_id"]] = spent.get(r["profile_id"], 0.0) + (
                    (r.get("cost_usd") or 0) if bench["basis"] == "usd" else (r.get("proxy_units") or 0))
        for profile, amount in spent.items():
            point = next((p for p in block["points"] if p["profile_id"] == profile), None)
            if (profile == best["profile_id"] or not point or point["n"] < MIN_N or not point["cost_per_solved"]
                    or best["rate"] < (point["rate"] or 0) - 0.05):
                continue
            ratio = 1 - best["cost_per_solved"] / point["cost_per_solved"]
            if ratio >= SWITCH_MARGIN:
                items.append({"kind": kind, "from": profile, "to": best["profile_id"], "saving_ratio": round(ratio, 3),
                              "saving": round(amount * ratio, 6)})
                saving_high += amount * ratio
                saving_low += amount * ratio * 0.5       # the benchmark is noisy: report half as the cautious end
    return {"unit": bench["unit"], "actual": round(actual, 6), "items": items,
            "saving": {"low": round(saving_low, 6), "high": round(saving_high, 6)} if items else None}


# -- horizon and forecast ---------------------------------------------------------------------

def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def horizon(summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How far runs actually get: the share that reaches the end, hits the iteration budget, or needs a human."""
    done = [s for s in summaries if s["outcome"] != "RUNNING" and s["iterations"] > 0]
    if len(done) < MIN_RUNS_HORIZON:
        return {"runs": len(done), "enough": False,
                "text": f"Za mało zakończonych zadań ({len(done)} z {MIN_RUNS_HORIZON}), by powiedzieć, jak daleko "
                        "AAW dochodzi bez człowieka."}
    counts = Counter(s["outcome"] for s in done)
    iterations = [s["iterations"] for s in done]
    stops = Counter(s["why"] for s in done if s["outcome"] == "ESCALATED")
    buckets = {"1-3": [], "4-8": [], "9+": []}
    for s in done:
        buckets["1-3" if s["iterations"] <= 3 else "4-8" if s["iterations"] <= 8 else "9+"].append(s["outcome"] == "FINISHED")
    return {"runs": len(done), "enough": True, "outcomes": dict(counts),
            "finished_share": round(counts["FINISHED"] / len(done), 2),
            "iterations": {"median": _quantile(iterations, .5), "p25": _quantile(iterations, .25),
                           "p75": _quantile(iterations, .75), "max": max(iterations)},
            "stop_reasons": dict(stops.most_common(5)),
            "by_size": {k: {"runs": len(v), "finished_share": round(sum(v) / len(v), 2) if v else None}
                        for k, v in buckets.items()},
            "text": f"{counts['FINISHED']} z {len(done)} zadań doszło do końca w jednym ciągu, mediana "
                    f"{_quantile(iterations, .5):.0f} iteracji; {counts['ESCALATED']} wymagało człowieka"
                    + (f" (najczęściej: {stops.most_common(1)[0][0]})" if stops else "") + "."}


def forecast(state: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """An honest range for the rest of a running run - or an explicit 'no estimate'."""
    roadmap = state.get("roadmap") or {}
    pending = [k for k, r in roadmap.items() if r.get("status") == "PENDING" and not r.get("recurring")]
    standing = any(r.get("recurring") and r.get("status") == "PENDING" for r in roadmap.values())
    chain = state.get("chain_state") or {}
    mine = [r for r in rows if r.get("solved") is not None or r.get("status") == "PROVISIONAL"]
    own = [r for r in mine if r.get("total_proxy_units") or r.get("total_cost_usd")]
    basis = stats.choose_basis(scoped(own, "total"), "auto") if own else "proxy"
    source, sample = None, []
    if len(own) >= 2:
        source, sample = "TEN PRZEBIEG", own
    else:
        kinds = Counter(r["kind"] for r in rows) or Counter()
        same = [r for r in history if r.get("solved") is not None and (not kinds or r["kind"] in kinds)
                and (r.get("total_proxy_units") or r.get("total_cost_usd"))]
        if len(same) >= MIN_N:
            source, sample = "HISTORIA", same
            basis = stats.choose_basis(scoped(same, "total"), "auto")
    out: dict[str, Any] = {"pending_items": len(pending), "open_ended": standing, "chains_closed": chain.get("closed_chains", 0),
                           "polish_expected": bool(state.get("deferred_findings")) and not state.get("polish_done")}
    if not pending and not standing:
        out.update({"estimate": None, "note": "Nie zostały żadne punkty roadmapy do zrobienia."})
        return out
    if not sample:
        out.update({"estimate": None, "note": "Za mało danych, by uczciwie oszacować czas i koszt — pokażę je po kilku iteracjach."})
        return out
    key = "total_cost_usd" if basis == "usd" else "total_proxy_units"
    wall = [r.get("total_wall_s") for r in sample if r.get("total_wall_s")]
    cost = [r[key] for r in sample if r.get(key) is not None]
    left = len(pending)
    out["estimate"] = {"source": source, "n": len(sample), "unit": "USD" if basis == "usd" else "proxy",
                       "iterations_left": left,
                       "cost": None if not cost else {"low": round(_quantile(cost, .25) * left, 6),
                                                      "mid": round(_quantile(cost, .5) * left, 6),
                                                      "high": round(_quantile(cost, .75) * left, 6)},
                       "wall_s": None if not wall else {"low": round(_quantile(wall, .25) * left),
                                                        "mid": round(_quantile(wall, .5) * left),
                                                        "high": round(_quantile(wall, .75) * left)}}
    if standing:
        out["note"] = "Zadanie ma stałą pozycję „kontynuuj”: koniec zależy od decyzji planera, szacunek dotyczy tylko wypisanych punktów."
    return out


def forecast_for_goal(goal: str, directions: Sequence[str], bench: Mapping[str, Any], history: Sequence[Mapping[str, Any]],
                      names: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Before START: what the user's own history says about work like this goal."""
    kind = classify_task(goal, criteria=list(directions))
    block = bench["kinds"].get(kind["kind"])
    best = next((p for p in (block or {}).get("points", []) if p["is_best"]), None)
    same = [r for r in history if r["kind"] == kind["kind"] and r.get("solved") is not None]
    # the per-iteration range is an all-in figure, so it picks its own basis: USD only when every call was priced
    basis = stats.choose_basis(scoped(same, "total"), "auto")
    key = "total_cost_usd" if basis == "usd" else "total_proxy_units"
    cost = [r[key] for r in same if r.get(key) is not None]
    out = {"kind": kind["kind"], "kind_label": KIND_LABEL[kind["kind"]], "kind_confidence": kind["confidence"],
           "n": len(same), "unit": "USD" if basis == "usd" else "proxy", "recommended": None, "per_iteration": None,
           "recommended_unit": bench["unit"]}
    if best:
        out["recommended"] = {"profile_id": best["profile_id"], "label": best["label"], "rate": best["rate"],
                              "cost_per_solved": best["cost_per_solved"], "n": best["n"], "confidence": best["confidence"]}
    if len(cost) >= MIN_N:
        out["per_iteration"] = {"low": round(_quantile(cost, .25), 6), "mid": round(_quantile(cost, .5), 6),
                                "high": round(_quantile(cost, .75), 6)}
    if out["per_iteration"] or out["recommended"]:
        out["note"] = None
    elif same:
        out["note"] = (f"Masz już {len(same)} iteracji typu „{KIND_LABEL[kind['kind']]}”, ale jeszcze za mało z pełnym "
                       "kosztem, by podać widełki — dojdą same.")
    else:
        out["note"] = f"To pierwsze zadania typu „{KIND_LABEL[kind['kind']]}” — AAW zbierze dane sam, nic nie musisz robić."
    return out


# -- roadmap forecast: how far, how much, before START ----------------------------------------------

def _range(values: Sequence[float]) -> dict[str, float] | None:
    return None if not values else {"low": _quantile(values, .25), "mid": _quantile(values, .5), "high": _quantile(values, .75)}


def forecast_for_roadmap(items: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]], *,
                         horizon_stats: Mapping[str, Any] | None = None, chain_length: int | None = None,
                         max_iterations: int = 40, continuous: bool = False) -> dict[str, Any]:
    """What a whole roadmap is likely to take - from the user's own history, as ranges, never as a promise.

    Each item is one iteration. Its cost/time is the all-in median (p25-p75) of past iterations of the same task kind,
    else of all past iterations, else unknown. Sums of quantiles are deliberately wide (conservative). Items marked
    human-required are listed as gates: AAW will stop before them.
    """
    decided = [r for r in history if r.get("solved") is not None]
    rows_for: list[tuple[str, list[Mapping[str, Any]]]] = []
    out_items: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("kind") if item.get("kind") in KINDS else classify_task(item.get("title"))["kind"]
        same = [r for r in decided if r["kind"] == kind and (r.get("total_proxy_units") or r.get("total_cost_usd"))]
        if len(same) >= MIN_N:
            source, rows = "KIND", same
        else:
            everything = [r for r in decided if r.get("total_proxy_units") or r.get("total_cost_usd")]
            source, rows = ("ALL", everything) if len(everything) >= MIN_N else ("NONE", [])
        rows_for.append((source, rows))
        out_items.append({"title": item.get("title"), "kind": kind, "kind_label": KIND_LABEL[kind],
                          "human_required": bool(item.get("human_required")), "size": item.get("size"),
                          "source": source, "n": len(rows)})
    used = [r for _, rows in rows_for for r in rows]
    basis = stats.choose_basis(scoped(used, "total"), "auto") if used else "proxy"
    ckey, wkey = ("total_cost_usd" if basis == "usd" else "total_proxy_units"), "total_wall_s"
    lows = mids = highs = 0.0
    wl = wm = wh = 0.0
    covered = 0
    for entry, (source, rows) in zip(out_items, rows_for):
        cost, wall = _range([r[ckey] for r in rows if r.get(ckey) is not None]), _range([r[wkey] for r in rows if r.get(wkey)])
        entry["cost"], entry["wall_s"] = (None, None) if entry["human_required"] else (cost, wall)
        if entry["human_required"]:
            continue
        if cost:
            covered += 1
            lows, mids, highs = lows + cost["low"], mids + cost["mid"], highs + cost["high"]
        if wall:
            wl, wm, wh = wl + wall["low"], wm + wall["mid"], wh + wall["high"]
    n = sum(1 for i in out_items if not i["human_required"])
    total = {"iterations": n, "covered_items": covered, "human_items": len(out_items) - n, "unit": "USD" if basis == "usd" else "proxy",
             "cost": {"low": round(lows, 6), "mid": round(mids, 6), "high": round(highs, 6)} if covered else None,
             "wall_s": {"low": round(wl), "mid": round(wm), "high": round(wh)} if covered and wm else None}
    chains = None
    if chain_length and n:
        count = -(-n // chain_length)
        chains = {"length": chain_length, "count": count, "serious_reviews": count, "polish": True}
    gates = [{"title": i["title"], "reason": "wymaga Twojej decyzji — AAW zatrzyma się przed tym punktem"}
             for i in out_items if i["human_required"]]
    horizon_stats = horizon_stats or {}
    reach: dict[str, Any] = {"enough": False, "text": horizon_stats.get("text") or
                             "Za mało zakończonych zadań, by oszacować szansę dojścia do końca."}
    if horizon_stats.get("enough"):
        bucket = "1-3" if n <= 3 else "4-8" if n <= 8 else "9+"
        sized = (horizon_stats.get("by_size") or {}).get(bucket) or {}
        if sized.get("runs", 0) >= MIN_RUNS_HORIZON and sized.get("finished_share") is not None:
            share, scope, runs = sized["finished_share"], f"zadań wielkości {bucket} iteracji", sized["runs"]
        else:
            share, scope, runs = horizon_stats["finished_share"], "wszystkich zadań", horizon_stats["runs"]
        reach = {"enough": True, "finished_share": share, "runs": runs, "bucket": bucket,
                 "text": f"{share * 100:.0f}% {scope} doszło do końca bez człowieka ({runs} zakończonych)."}
    warnings = []
    if n > max_iterations:
        warnings.append(f"Roadmapa ma {n} punktów, a bezpiecznik iteracji to {max_iterations} — AAW zatrzyma się wcześniej.")
    if covered < n:
        warnings.append(f"Widełki obejmują {covered} z {n} punktów — dla reszty brak danych z historii.")
    return {"items": out_items, "total": total, "chains": chains, "gates": gates, "reach": reach,
            "open_ended": continuous, "warnings": warnings,
            "note": "Widełki są celowo szerokie (sumują kwartyle) i opierają się wyłącznie na Twojej historii."}


# -- exploration plan: which benchmark cells are still thin ---------------------------------------------

def exploration_plan(rows: Sequence[Mapping[str, Any]], candidates: Sequence[str], *, exclude: Sequence[str] = ("INFRA",),
                     min_n: int = MIN_N, guard_n: int = 5, guard_rate: float = 0.5) -> dict[str, Any]:
    """Per task kind, the candidate profiles with fewer than `min_n` decided trials, thinnest first.

    A candidate that already has >= `guard_n` decided trials overall with a solve rate below `guard_rate` is left out
    entirely: exploration is for filling thin cells, not for repeating known failures.
    """
    decided = [r for r in rows if r.get("solved") is not None and r.get("profile_id")]
    per_profile: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in decided:
        per_profile[r["profile_id"]].append(r)
    poor = {p for p in candidates if len(per_profile[p]) >= guard_n
            and sum(1 for r in per_profile[p] if r["solved"]) / len(per_profile[p]) < guard_rate}
    counts = Counter((r["kind"], r["profile_id"]) for r in decided)
    wanted: dict[str, list[str]] = {}
    cells: list[dict[str, Any]] = []
    for kind in KINDS:
        if kind in exclude:
            continue
        thin = sorted((c for c in candidates if c not in poor and counts[(kind, c)] < min_n), key=lambda c: (counts[(kind, c)], c))
        if thin:
            wanted[kind] = thin
            cells.extend({"kind": kind, "profile_id": c, "n": counts[(kind, c)]} for c in thin)
    return {"wanted": wanted, "cells": cells, "skipped_poor": sorted(poor)}


# -- the run as a journey ---------------------------------------------------------------------

_FEED = {"ITERATION_PLANNED": "Zaplanowano iterację", "ITERATION_PROVISIONALLY_ACCEPTED": "Iteracja gotowa (wstępnie)",
         "CHAIN_ACCEPTED": "Review serii zaakceptował cały łańcuch", "CHAIN_CLOSE_STARTED": "Zaczyna się review serii",
         "FINDINGS_DEFERRED": "Drobne uwagi odłożone na polerowanie", "POLISH_PLANNED": "Polerowanie odłożonych uwag",
         "REPAIR_COMPLETED": "Naprawa zakończona", "ITERATION_ACCEPTED": "Iteracja zaakceptowana",
         "ESCALATED": "AAW zatrzymał się i prosi o decyzję", "AWAITING_HUMAN": "Czeka na Twoją decyzję",
         "CHAIN_PLAN_RECORDED": "Plan całej serii zapisany", "CHAIN_PLAN_STUB_REJECTED": "Plan serii nieaktualny — planuję od nowa",
         "EXPLORATION_SELECTED": "Eksploracja: ten krok wykonuje inny model, by zebrać dane",
         "EXPLORATION_SKIPPED": "Eksploracja pominięta (model niedostępny)", "RUN_PAUSED": "Wstrzymano", "RUN_RESUMED": "Wznowiono", "ROADMAP_DECISION": "Decyzja o dalszym ciągu roadmapy"}


def _feed(events: Sequence[Mapping[str, Any]], limit: int = 14) -> list[dict[str, Any]]:
    rows = []
    for ev in reversed(events):
        kind = ev.get("event_type")
        if kind == "PHASE_COMPLETED":
            text = f"Zakończono etap {ev.get('phase')}"
        elif kind in _FEED:
            text = _FEED[kind]
        else:
            continue
        rows.append({"at": ev.get("occurred_at"), "event": kind, "text": text, "phase": ev.get("phase")})
        if len(rows) >= limit:
            break
    return rows


_REVIEW_PHASES = ("AWAITING_REVIEW", "REVIEW", "FINAL_REVIEW", "REPAIR", "SELF_VERIFY")


def journey(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """From idea to the Human Gate: where this run is on the whole path - and, after an escalation, where it stopped."""
    phase, status = state.get("phase"), state.get("status")
    its = state.get("iterations") or []
    last = its[-1] if its else {}
    mode = (last.get("chain") or {}).get("review_mode")
    cfg_on = bool(its and last.get("chain")) or bool((state.get("roles") or {}).get("chain", {}).get("enabled"))
    escalation = state.get("escalation") or {}
    stopped_in = escalation.get("from_phase")
    closes = (state.get("chain_state") or {}).get("closed_chains", 0)
    running = status == "RUNNING"
    at_gate = status in ("AWAITING_HUMAN", "HUMAN_APPROVED", "PROMOTED", "REJECTED")
    architect = state.get("directional_charter") is not None or bool(its)
    closing = mode == "CHAIN_CLOSE" and running and phase in _REVIEW_PHASES
    polishing = mode == "POLISH" and running
    failed_close = bool(escalation) and mode == "CHAIN_CLOSE" and stopped_in in _REVIEW_PHASES
    failed_polish = bool(escalation) and mode == "POLISH"
    failed_chains = bool(escalation) and architect and not failed_close and not failed_polish
    failed_charter = bool(escalation) and not architect
    open_deferred = sum(1 for f in state.get("deferred_findings", []) if f.get("status") == "OPEN")

    def stage(id_: str, label: str, state_: str, detail: str) -> dict[str, Any]:
        return {"id": id_, "label": label, "state": state_, "detail": detail}

    return [
        stage("idea", "Pomysł i mandat", "done", "zamrożony kierunek"),
        stage("charter", "Plan początkowy", "failed" if failed_charter else "done" if architect else
              ("active" if running else "pending"), "najsilniejszy model ustala kierunek"),
        stage("chains", "Łańcuchy implementacji",
              "failed" if failed_chains else "done" if (at_gate or closing or polishing or failed_close or failed_polish) else
              ("active" if running else "pending"),
              f"{closes} zamkniętych" if cfg_on else f"{len(its)} iteracji"),
        stage("close", "Review serii", "failed" if failed_close else "active" if closing else
              "done" if (at_gate and not escalation) or polishing or closes else "pending",
              "mocny model patrzy na cały łańcuch" if cfg_on else "po każdej iteracji"),
        stage("polish", "Polerowanie", "failed" if failed_polish else "active" if polishing else
              "done" if state.get("polish_done") else "pending", f"{open_deferred} uwag w kolejce"),
        stage("gate", "Twoja decyzja", "active" if at_gate else "pending", "bez merge i push"),
    ]


def chain_slots(state: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    cs = state.get("chain_state") or {}
    its = state.get("iterations") or []
    length = ((its[-1].get("chain") or {}).get("length") if its and its[-1].get("chain") else None) or \
        (state.get("roles") or {}).get("chain", {}).get("length")
    if not length:
        return None
    chain_id = cs.get("chain_id", 1)
    cost = {r["iteration_id"]: r.get("total_cost_usd") if r.get("total_cost_usd") is not None else r.get("total_proxy_units")
            for r in rows}
    explored = {r["iteration_id"] for r in rows if r.get("explored")}
    members = [i for i in its if (i.get("chain") or {}).get("chain_id") == chain_id]
    slots = []
    for position in range(1, length + 1):
        it = next((m for m in members if (m.get("chain") or {}).get("position") == position), None)
        slots.append({"position": position, "closing": position == length,
                      "state": "pending" if not it else ("accepted" if it["status"] == "ACCEPTED" else
                                                         "provisional" if it["status"] == "PROVISIONAL" else
                                                         "failed" if it["status"] == "ESCALATED" else "active"),
                      "iteration": it.get("index") if it else None, "cost": cost.get(it["iteration_id"]) if it else None,
                      "explored": bool(it and it["iteration_id"] in explored),
                      "goal": ((it.get("plan") or {}).get("goal") if it else None)})
    return {"chain_id": chain_id, "length": length, "slots": slots, "closed": cs.get("closed_chains", 0)}


def meter(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary = tel.summarize(records)
    total = summary["total"]
    return {"calls": summary["records"], "input_tokens": total["input_tokens"], "cached_tokens": total["cached_tokens"],
            "output_tokens": total["output_tokens"], "proxy_units": total["proxy_units"], "cost_usd": total["cost_usd"],
            "wall_s": total["wall_s"], "by_category": {k: v["proxy_units"] for k, v in summary["by_category"].items()},
            "share": summary["share_of_proxy_units"], "failed_calls": summary["failed_calls"],
            "unpriced_calls": summary["cost_coverage"]["unpriced_calls"]}


def live_view(state: Mapping[str, Any], events: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]],
              rows: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    flight = state.get("in_flight") or {}
    return {"journey": journey(state), "chain": chain_slots(state, rows), "meter": meter(records),
            "feed": _feed(events), "forecast": forecast(state, rows, history),
            "now": ({"executor": flight.get("executor"), "role": flight.get("role"), "started_at": flight.get("started_at"),
                     "profile": (flight.get("execution_ref") or {}).get("profile")} if flight else None),
            "deferred_open": sum(1 for f in state.get("deferred_findings", []) if f.get("status") == "OPEN")}


# -- demo (clearly labelled synthetic preview, never mixed with real rows) --------------------------

PERSONAS = {
    "backend": ("Inżynier backendu", {"BUGFIX": .35, "REFACTOR": .2, "TESTS": .2, "FEATURE": .2, "INFRA": .05}),
    "web": ("Twórca stron", {"UI": .45, "DOCS": .2, "FEATURE": .25, "BUGFIX": .1}),
    "researcher": ("Badacz", {"ANALYSIS": .5, "FEATURE": .2, "DOCS": .2, "TESTS": .1}),
}
_DEMO_PROFILES = {   # profile -> (cost scale, solve probability by kind family)
    "GPT6_LUNA_HIGH": (1.0, {"easy": .88, "mid": .62, "hard": .35}),
    "GPT6_LUNA_MAX": (2.2, {"easy": .93, "mid": .82, "hard": .6}),
    "CLAUDE_SONNET_5_5_MEDIUM": (14.0, {"easy": .96, "mid": .92, "hard": .8}),
    "SOL_HIGH": (9.0, {"easy": .94, "mid": .86, "hard": .66}),
}
_FAMILY = {"DOCS": "easy", "UI": "mid", "BUGFIX": "mid", "TESTS": "easy", "REFACTOR": "mid", "FEATURE": "mid",
           "INFRA": "hard", "ANALYSIS": "hard", "OTHER": "mid"}


DEMO_NAMES = {"GPT6_LUNA_HIGH": "GPT-6 Luna / high", "GPT6_LUNA_MAX": "GPT-6 Luna / max",
              "CLAUDE_SONNET_5_5_MEDIUM": "Claude Sonnet 5.5 / medium", "SOL_HIGH": "GPT-5.6 Sol / high"}


def demo_rows(persona: str = "backend", n: int = 200) -> list[dict[str, Any]]:
    """Synthetic rows that exercise every view. Deterministic; labelled as a preview by the API and the UI."""
    mix = PERSONAS.get(persona, PERSONAS["backend"])[1]
    rows, seed = [], sum(map(ord, persona)) * 7919
    kinds = [k for k, w in mix.items() for _ in range(max(1, round(w * 20)))]
    profile_ids = list(_DEMO_PROFILES)
    for i in range(n):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        kind = kinds[seed % len(kinds)]
        # the user tends to use a favourite profile, and sometimes tries another
        profile = profile_ids[0] if (seed >> 8) % 100 < 34 else profile_ids[(seed >> 4) % len(profile_ids)]
        scale, probs = _DEMO_PROFILES[profile]
        p = probs[_FAMILY[kind]]
        solved = ((seed >> 12) % 1000) / 1000 < p
        cost = 0.008 * scale * (1 + ((seed >> 3) % 40) / 80) * (1 if solved else 1.6)
        rows.append({"schema": ROW_SCHEMA, "run_id": f"DEMO_{i // 6}", "iteration_id": f"DEMO_IT_{i}", "index": i % 6 + 1,
                     "kind": kind, "kind_confidence": 1.0, "difficulty": "MEDIUM", "task_id": f"DEMO:{i}",
                     "profile_id": profile, "model": profile.lower(), "solved": solved, "status": "ACCEPTED" if solved else "ESCALATED",
                     "repairs": 0 if solved else 1, "review_mode": "CHAIN_CLOSE" if i % 8 == 7 else "LIGHT",
                     "cost_usd": round(cost, 6), "proxy_units": round(cost * 12000, 1), "wall_s": 90 + (seed % 200),
                     "total_cost_usd": round(cost * 1.9, 6), "total_proxy_units": round(cost * 12000 * 1.9, 1),
                     "total_wall_s": 140 + (seed % 260), "calls": 4, "source": "DEMO"})
    return rows
