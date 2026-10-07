#!/usr/bin/env python3
"""AAW PRODUCT MVP V0.2 — read-only projections for the user.

Every value here is computed on read from the engine's own artifacts:
`AUTONOMY/autonomy_state.json`, `AUTONOMY/autonomy_events.jsonl`, the V0.4B
ledger, the per-execution result artifacts and descriptors, and the run
lock. Nothing is written, cached as truth, or fed back to the engine. A brief
is a projection of those artifacts and always links to the raw evidence it
was built from.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Mapping

import aaw_experience as ex
import aaw_telemetry as tel
import autonomy_contract as ac
import autonomy_run_lock as rl
import product_home
import product_runs as prun

PIPELINE = [("PLAN", "Plan"), ("EXECUTE", "Implementacja"), ("SELF_VERIFY", "Weryfikacja"),
            ("REVIEW", "Review"), ("REPAIR", "Naprawa"), ("FINAL_REVIEW", "Final review"),
            ("NEXT", "Następna iteracja")]
NODE_OF_PHASE = {"PLAN": "PLAN", "EXECUTE": "EXECUTE", "SELF_VERIFY": "SELF_VERIFY", "AWAITING_REVIEW": "REVIEW",
                 "REVIEW": "REVIEW", "REPAIR": "REPAIR", "FINAL_REVIEW": "FINAL_REVIEW", "ROADMAP_CHECK": "NEXT"}
ACTIVITY = {
    "PLAN": "Planowanie kolejnej iteracji", "EXECUTE": "Implementacja zmian",
    "SELF_VERIFY": "Weryfikacja wyników", "AWAITING_REVIEW": "Przygotowanie materiału do review",
    "REVIEW": "Review implementacji", "REPAIR": "Naprawa uwag z review", "FINAL_REVIEW": "Final review",
    "ROADMAP_CHECK": "Sprawdzanie, co dalej w roadmapie",
}

# User-facing status categories (Home sections).
S_RUNNING, S_STOPPING, S_PAUSED, S_INTERRUPTED = "RUNNING", "STOPPING", "PAUSED", "INTERRUPTED"
S_STARTING, S_START_FAILED = "STARTING", "START_FAILED"
S_GATE, S_ATTENTION = "READY_FOR_DECISION", "NEEDS_ATTENTION"
S_ACCEPTED, S_REJECTED = "ACCEPTED", "REJECTED"
HOME_SECTION = {S_RUNNING: "running", S_STOPPING: "running", S_STARTING: "running",
                S_PAUSED: "paused", S_INTERRUPTED: "paused", S_START_FAILED: "attention",
                S_GATE: "attention", S_ATTENTION: "attention", S_ACCEPTED: "completed", S_REJECTED: "completed"}
STATUS_LABEL = {S_RUNNING: "Pracuje", S_STOPPING: "Zatrzymywanie…", S_STARTING: "Uruchamianie…",
                S_PAUSED: "Wstrzymane", S_INTERRUPTED: "Przerwane — można bezpiecznie wznowić",
                S_START_FAILED: "Nie wystartowało", S_GATE: "Czeka na Twoją decyzję",
                S_ATTENTION: "Wymaga uwagi", S_ACCEPTED: "Zaakceptowane", S_REJECTED: "Odrzucone"}

ESCALATION_TEXT = {
    ac.E_ROLE_UNAVAILABLE: "Model wymagany na tym etapie jest niedostępny na tym komputerze (AAW nigdy nie podmienia go po cichu).",
    ac.E_INTERRUPTED: "Etap zmieniający pliki został przerwany — jego skutki w kopii roboczej są niepewne, więc AAW nie powtórzył go automatycznie.",
    ac.E_REVIEW: "Reviewer nie mógł potwierdzić poprawności i przekazał decyzję człowiekowi.",
    ac.E_REVIEW_INVALID: "Reviewer zwrócił nieprawidłową odpowiedź — AAW nie traktuje tego jako PASS.",
    ac.E_REPAIR_LIMIT: "Kolejne naprawy nie doprowadziły do akceptacji w dozwolonym limicie.",
    ac.E_NO_PROGRESS: "Naprawy nie rozwiązały problemu mimo automatycznej eskalacji (wyższy wysiłek, mocniejszy model, diagnoza) — drabina eskalacji wyczerpana.",
    ac.E_ROUTING: "Żaden odpowiedni model nie był dostępny (limity, zaufanie, możliwości lub awaria dostawcy) — nic nie zostało uruchomione.",
    ac.E_GIT: "Wykryto zmianę poza izolowaną kopią roboczą (np. ktoś edytował pliki w głównym folderze projektu albo gałąź main się zmieniła). AAW zatrzymał się, żeby niczego nie nadpisać.",
    ac.E_EXECUTOR: "Wywołanie modelu nie powiodło się (błąd CLI, limit czasu lub nieprawidłowa odpowiedź).",
    ac.E_EXECUTOR_TIMEOUT: "Wywołanie modelu przekroczyło limit czasu. AAW zachował logi, wynik procesu i zmiany w izolowanej kopii.",
    ac.E_PLANNER: "Planista uznał, że dalsza praca wymaga decyzji człowieka.",
    ac.E_SCOPE: "Plan wychodził poza zakres zadania.",
    ac.E_EXTENSION: "Planista próbował zmienić lub rozszerzyć zadanie — to wymaga decyzji człowieka.",
    ac.E_DECISION: "Plan wymagał decyzji produktowej, którą może podjąć tylko człowiek.",
    ac.E_LINK: "Plan nie był powiązany z roadmapą zadania.",
    ac.E_PLAN_INVALID: "Planista zwrócił nieprawidłowy plan.",
    ac.E_MANDATE_MISMATCH: "Plan nie odnosił się do zamrożonego zadania.",
    ac.E_RECONCILE: "Stan wywołania jest niejednoznaczny — potrzebna decyzja człowieka.",
    ac.E_LEDGER: "Nie udało się trwale zapisać dowodu wywołania — nic nie zostało uruchomione.",
    ac.E_SESSION_REUSE: "Wywołanie nie miało świeżego kontekstu — niezależność review nieudowodniona.",
    ac.E_VERIFY_MUTATION: "Weryfikacja zmieniła pliki, choć miała tylko czytać.",
    ac.E_UNEXPLAINED_END: "Planista zakończył pracę bez uzasadnienia pozostałych punktów.",
}
HOLD_TEXT = {
    ac.HOLD_ROADMAP_EXHAUSTED: "Roadmapa wyczerpana — wszystkie punkty, które AAW mógł wykonać samodzielnie, są zrobione.",
    ac.HOLD_ITERATION_CAP: ("Osiągnięto bezpiecznik liczby iteracji ustawiony dla tego zadania, a w roadmapie jest jeszcze "
                            "praca. Możesz zaakceptować wynik albo uruchomić kolejne zadanie z tego punktu."),
}


def _parse_time(value: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _seconds_between(start: str | None, end: str | None = None) -> int | None:
    a = _parse_time(start)
    b = _parse_time(end) if end else dt.datetime.now().astimezone()
    if not a or not b:
        return None
    return max(0, int((b - a).total_seconds()))


def _journal(run_id: str) -> list[dict[str, Any]]:
    path = prun.autonomy_dir(run_id) / "autonomy_events.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"event_type": "UNREADABLE_LINE"})
    return rows


def _ledger(run_id: str) -> dict[str, Any]:
    path = prun.run_dir(run_id) / "LEDGER" / "execution_events.jsonl"
    if not path.is_file():
        return {}
    from execution_ledger import ExecutionLedger
    try:
        return ExecutionLedger.for_run(run_id, prun.runs_root()).lifecycle()
    except Exception:
        return {}


def _result(run_id: str, execution_id: str | None) -> dict[str, Any]:
    if not execution_id:
        return {}
    return product_home.read_json(prun.autonomy_dir(run_id) / "RESULTS" / f"{execution_id}.json", {}) or {}


def _state(run_id: str) -> dict[str, Any] | None:
    return product_home.read_json(prun.autonomy_dir(run_id) / "autonomy_state.json")


def _profile_label(profile_id: str | None, model: str | None = None, effort: str | None = None) -> str | None:
    if not profile_id:
        return None
    import product_recommendations as pr
    name = pr.profile_display(profile_id)
    detail = ", ".join(x for x in (model, effort) if x)
    return f"{name} ({detail})" if detail else name


# ── status ───────────────────────────────────────────────────────────────────

def classify(run_id: str, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _state(run_id)
    pdir = prun.product_dir(run_id)
    exit_record = product_home.read_json(pdir / "worker_exit.json") or {}
    stop = product_home.read_json(pdir / "stop_request.json")
    lock = rl.inspect_lock(prun.autonomy_dir(run_id))
    alive = prun.worker_alive(run_id)
    info: dict[str, Any] = {"worker_alive": alive, "lock": {"held": lock["held"], "outcome": lock["outcome"],
                            "liveness": lock["liveness"],
                            "owner_token": (lock["owner"] or {}).get("owner_token")},
                            "stop_request": stop, "worker_exit": exit_record or None}
    if not state:
        if alive:
            return {**info, "status": S_STARTING}
        if exit_record.get("status") == "WORKER_ERROR":
            return {**info, "status": S_START_FAILED, "detail": exit_record.get("error")}
        return {**info, "status": S_STARTING if (pdir / "worker_launch.json").exists() else S_START_FAILED}
    status = state.get("status")
    if status == ac.RUNNING:
        if alive:
            return {**info, "status": S_STOPPING if stop else S_RUNNING}
        events = _journal(run_id)
        last = next((e for e in reversed(events) if e.get("event_type") in
                     ("RUN_PAUSED", "RUN_CANCELLED_IN_FLIGHT", "RUN_RESUMED", "PHASE_STARTED", "PHASE_COMPLETED")), {})
        if last.get("event_type") in ("RUN_PAUSED", "RUN_CANCELLED_IN_FLIGHT") and not lock["held"]:
            return {**info, "status": S_PAUSED, "pause_event": last}
        return {**info, "status": S_INTERRUPTED,
                "detail": exit_record.get("error") or "Proces roboczy AAW zakończył się bez zapisanego zatrzymania "
                          "(np. zamknięcie komputera lub awaria). Możesz bezpiecznie wznowić."}
    if status == ac.AWAITING_HUMAN:
        hold = state.get("hold") or {}
        return {**info, "status": S_GATE if hold.get("promotable") else S_ATTENTION}
    if status in (ac.HUMAN_APPROVED, ac.PROMOTED):
        return {**info, "status": S_ACCEPTED}
    return {**info, "status": S_REJECTED}


# ── process view ─────────────────────────────────────────────────────────────

def _iteration_nodes(state: Mapping[str, Any], events: list[dict[str, Any]], iteration_id: str | None,
                     current_phase: str | None, running: bool,
                     failed_phase: str | None = None) -> list[dict[str, Any]]:
    completed: dict[str, int] = {}
    for event in events:
        if event.get("iteration_id") != iteration_id:
            continue
        if event.get("event_type") == "PHASE_COMPLETED":
            node = NODE_OF_PHASE.get(event.get("phase") or "")
            if node and event.get("phase") != "AWAITING_REVIEW":
                completed[node] = completed.get(node, 0) + 1
    active = NODE_OF_PHASE.get(current_phase or "") if current_phase else None
    failed = NODE_OF_PHASE.get(failed_phase or "") if failed_phase else None
    nodes = []
    for key, label in PIPELINE:
        count = completed.get(key, 0)
        if key == failed:
            mark = "failed"
        elif key == active:
            mark = "active" if running else "stopped"
        elif count:
            mark = "done"
        else:
            mark = "pending"
        nodes.append({"key": key, "label": label, "state": mark, "count": count})
    return nodes


def process_view(run_id: str, state: Mapping[str, Any], status: Mapping[str, Any],
                 events: list[dict[str, Any]]) -> dict[str, Any]:
    phase = state.get("phase")
    running = status["status"] in (S_RUNNING, S_STOPPING)
    iterations = state.get("iterations") or []
    planning = state.get("planning") or {}
    if phase == ac.PLAN and planning:
        index, iteration_id = planning.get("index"), planning.get("iteration_id")
    elif iterations:
        index, iteration_id = iterations[-1].get("index"), iterations[-1].get("iteration_id")
    else:
        index, iteration_id = 1, None
    in_flight_phase = phase if state.get("status") == ac.RUNNING else None
    escalation = state.get("escalation") or {}
    failed_phase = escalation.get("from_phase") if state.get("status") == ac.AWAITING_HUMAN and \
        escalation.get("iteration_id") in (None, iteration_id) else None
    nodes = _iteration_nodes(state, events, iteration_id, in_flight_phase, running, failed_phase)
    flight = state.get("in_flight") or {}
    ref = flight.get("execution_ref") or {}
    activity = ACTIVITY.get(phase or "", phase)
    if phase == ac.PLAN and state.get("directional_charter") is None and state.get("policy_preset"):
        activity = "Plan początkowy: zamrażanie kierunku i pierwsza iteracja"
    if status["status"] == S_STOPPING:
        mode = ((product_home.read_json(prun.product_dir(run_id) / "stop_effect.json") or {}).get("events") or [{}])[-1]
        if mode.get("mode") == "PAUSE_AT_NEXT_BOUNDARY":
            activity = f"Kończę bezpiecznie bieżący krok ({activity}), potem zatrzymam się"
        else:
            activity = "Zatrzymywanie…"
    roadmap = state.get("roadmap") or {}
    counted = {k: r for k, r in roadmap.items() if not r.get("recurring")}
    done = sum(1 for r in counted.values() if r.get("status") == ac.R_DONE)
    titles = {i["item_id"]: i["title"] for i in state["mandate"]["roadmap_mandate"]["items"]}
    standing = next(({"item_id": k, "status": r.get("status"), "iterations": len(r.get("iterations") or []),
                      "reason": r.get("reason")} for k, r in roadmap.items() if r.get("recurring")), None)
    current = next((n for n in nodes if n["state"] in ("active", "stopped", "failed")), None)
    phase_started = next((e.get("occurred_at") for e in reversed(events)
                          if e.get("event_type") == "PHASE_STARTED" and e.get("phase") == phase), None)
    return {
        "iteration": index, "iteration_id": iteration_id, "nodes": nodes, "phase": phase,
        "phase_label": current["label"] if current else None,
        "phase_elapsed_s": _seconds_between(phase_started) if running and phase_started else None,
        "activity": activity if state.get("status") == ac.RUNNING else None,
        "model": _profile_label(ref.get("profile"), ref.get("model"), ref.get("effort")) if flight else None,
        "role": flight.get("role"),
        "execution_id": flight.get("execution_id"),
        "step_elapsed_s": _seconds_between(flight.get("started_at")) if flight and running else None,
        "run_elapsed_s": _seconds_between(state.get("started_at"),
                                          None if running else state.get("updated_at")),
        "roadmap": {"done": done, "total": len(counted)},
        "standing": standing,
        "roadmap_items": [{"item_id": k, "title": titles.get(k, k), "status": r.get("status"),
                           "recurring": bool(r.get("recurring")), "reason": r.get("reason")}
                          for k, r in roadmap.items()],
        "iterations_done": len(state.get("iterations") or []),
        "max_iterations": state["mandate"]["roadmap_mandate"]["autonomy_bounds"]["max_iterations"],
        "eta": None,  # deliberately not shown: no data to estimate it honestly
    }


# ── briefs ───────────────────────────────────────────────────────────────────

def _checks(rows: Any) -> list[dict[str, Any]]:
    return [{"name": str(r.get("name")), "status": str(r.get("status", "")).upper(), "summary": r.get("summary")}
            for r in (rows or []) if isinstance(r, Mapping)]


def _exec_meta(state: Mapping[str, Any], ledger: Mapping[str, Any], execution_id: str | None) -> dict[str, Any]:
    ref = next((e for e in state.get("executions", []) if e.get("execution_id") == execution_id), {}) if execution_id else {}
    entry = ledger.get(execution_id) or {} if execution_id else {}
    close = (entry.get("closed") or [{}])[-1].get("payload", {}) if entry else {}
    return {"execution_id": execution_id,
            "model": _profile_label(ref.get("profile"), ref.get("model"), ref.get("effort")),
            "selection_reason": (ref.get("selection") or {}).get("selection_reason"),
            "ledger_state": entry.get("state"), "close_reason": close.get("close_reason"),
            "effect_certainty": close.get("effect_certainty")}


def _brief(kind: str, phase_label: str, meta: Mapping[str, Any], *, goal: str | None, done: list[str],
           checks: list[dict[str, Any]], problems: list[str], handed: list[str], at: str | None,
           evidence: list[str]) -> dict[str, Any]:
    return {"kind": kind, "phase": phase_label, "model": meta.get("model") or "kontroler AAW (deterministycznie)",
            "goal": goal, "done": [d for d in done if d], "checks": checks, "problems": [p for p in problems if p],
            "handed_over": [h for h in handed if h], "at": at, "execution_id": meta.get("execution_id"),
            "ledger_state": meta.get("ledger_state"), "close_reason": meta.get("close_reason"),
            "evidence_refs": [e for e in evidence if e]}


def _finding_text(finding: Mapping[str, Any]) -> str:
    where = f" [{finding.get('file')}]" if finding.get("file") else ""
    flag = "blokujące" if finding.get("blocking") else "nieblokujące"
    return f"{finding.get('severity', '?')} ({flag}){where}: {finding.get('summary')}"


def build_briefs(run_id: str, state: Mapping[str, Any], events: list[dict[str, Any]],
                 ledger: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Per iteration, the ordered phase briefs (projection of journal + state + artifacts)."""
    by_iteration: dict[str, list[dict[str, Any]]] = {}
    iterations = {it["iteration_id"]: it for it in state.get("iterations", [])}
    counters: dict[tuple[str, str], int] = {}

    def nth(iteration_id: str, key: str) -> int:
        counters[(iteration_id, key)] = counters.get((iteration_id, key), 0) + 1
        return counters[(iteration_id, key)] - 1

    roadmap_titles = {i["item_id"]: i["title"] for i in state["mandate"]["roadmap_mandate"]["items"]}
    for event in events:
        kind = event.get("event_type")
        iteration_id = event.get("iteration_id")
        it = iterations.get(iteration_id or "") or {}
        payload = event.get("payload") or {}
        at = event.get("occurred_at")
        brief = None
        if kind == "DIRECTIONAL_CHARTER_FROZEN":
            meta = _exec_meta(state, ledger, payload.get("initial_planner_execution_id"))
            charter = state.get("directional_charter") or {}
            risk = [f"{r['item_id']}: implementacja ≥ {r['implementation_floor']}, final review ≥ "
                    f"{r['final_review_floor']} — {r['reason']}" for r in charter.get("risk_guidance", [])]
            enforced = [f"{a['item_id']} ({'dodano' if a.get('action') == 'ADDED' else 'podniesiono'})"
                        for a in payload.get("risk_floor_adjustments") or []]
            brief = _brief("CHARTER", "Plan początkowy (architekt)", meta,
                           goal="Zamrozić kierunek całego zadania i warunki Human Gate",
                           done=[f"Zamrożono kierunek z {len(payload.get('roadmap_items', []))} punktami roadmapy",
                                 *(["Wskazówki ryzyka: " + "; ".join(risk)] if risk else []),
                                 *([f"Progi z Twoich ryzyk: {payload['mandated_risk_floors']} punktów roadmapy"
                                    + (f"; architekt je pominął lub obniżył, przywrócono: {', '.join(enforced)}"
                                       if enforced else "")] if payload.get("mandated_risk_floors") else [])],
                           checks=[], problems=[], at=at,
                           handed=["Zamrożony kierunek (hash) przekazany każdemu kolejnemu planiście i reviewerowi"],
                           evidence=[payload.get("initial_planner_execution_id")])
            iteration_id = (state.get("iterations") or [{}])[0].get("iteration_id") if state.get("iterations") else \
                (state.get("planning") or {}).get("iteration_id")
        elif kind == "ITERATION_PLANNED":
            plan = it.get("plan") or {}
            meta = _exec_meta(state, ledger, it.get("plan_execution_id"))
            refs = [f"{r}: {roadmap_titles.get(r, r)}" for r in payload.get("roadmap_refs", [])]
            brief = _brief("PLAN", "Plan", meta, goal=payload.get("goal"),
                           done=[f"Zakres: {'; '.join(refs) if refs else 'pierwsza iteracja z zadania'}",
                                 f"Uzasadnienie: {payload.get('scope_justification')}",
                                 f"Złożoność implementacji: {plan.get('implementation_complexity', 'NORMAL')}"],
                           checks=[{"name": "zakres zgodny z zadaniem", "status": "PASS", "summary": "scope guard"}],
                           problems=[f"Pominięto {s['item_id']}: {s['reason']}" for s in plan.get("skipped_items", [])],
                           handed=["Kryteria akceptacji: " + "; ".join(payload.get("acceptance_criteria", []))],
                           at=at, evidence=[it.get("plan_execution_id")])
        elif kind == "PHASE_COMPLETED" and event.get("phase") == "EXECUTE":
            meta = _exec_meta(state, ledger, payload.get("execution_id"))
            result = _result(run_id, payload.get("execution_id")).get("result") or it.get("execution") or {}
            brief = _brief("EXECUTE", "Implementacja", meta, goal=(it.get("plan") or {}).get("goal"),
                           done=[result.get("summary"),
                                 ("Zmienione pliki: " + ", ".join(result.get("changed_files", [])))
                                 if result.get("changed_files") else None],
                           checks=_checks(result.get("checks")),
                           problems=[*(f"Odstępstwo: {d}" for d in result.get("deviations", [])),
                                     *(f"Niepewność: {u}" for u in result.get("uncertainties", []))],
                           handed=["Zmiany w kopii roboczej → weryfikacja"], at=at, evidence=[payload.get("execution_id")])
        elif kind == "PHASE_COMPLETED" and event.get("phase") == "SELF_VERIFY":
            index = nth(iteration_id or "", "self_verify")
            entry = (it.get("self_verify") or [{}])[index] if index < len(it.get("self_verify") or []) else {}
            meta = _exec_meta(state, ledger, payload.get("execution_id"))
            failing = payload.get("failing") or entry.get("failing") or []
            brief = _brief("SELF_VERIFY", "Weryfikacja", meta, goal="Sprawdzić dowody i granice Git przed review",
                           done=[f"Tryb: {entry.get('mode', 'DETERMINISTIC')}", f"Sprawdzeń: {payload.get('checks')}"],
                           checks=_checks(entry.get("checks")),
                           problems=[f"Nie przeszło: {f}" for f in failing],
                           handed=["→ naprawa" if failing else "→ review"], at=at,
                           evidence=[payload.get("execution_id")])
        elif kind == "REVIEW_VERDICT":
            final = event.get("phase") == "FINAL_REVIEW"
            index = nth(iteration_id or "", "final" if final else "review")
            rows = it.get("final_reviews" if final else "reviews") or []
            review = rows[index] if index < len(rows) else {}
            meta = _exec_meta(state, ledger, payload.get("execution_id"))
            verdict = payload.get("verdict")
            downgraded = payload.get("downgraded_from")
            next_step = {"PASS": "→ final review" if not final else "→ iteracja zaakceptowana",
                         "REPAIR_REQUIRED": "→ naprawa wskazanych uwag", "ESCALATE": "→ decyzja człowieka"}
            brief = _brief("FINAL_REVIEW" if final else "REVIEW", "Final review" if final else "Review", meta,
                           goal="Niezależna ocena zmian na podstawie pakietu dowodów",
                           done=[f"Werdykt: {verdict}" + (f" (obniżony z {downgraded})" if downgraded else ""),
                                 payload.get("summary")],
                           checks=[], problems=[_finding_text(f) for f in payload.get("findings", [])] +
                           [f"Niepewność: {u}" for u in review.get("uncertainties", [])],
                           handed=[next_step.get(verdict, "")], at=at, evidence=[payload.get("execution_id")])
        elif kind == "REPAIR_COMPLETED":
            meta = _exec_meta(state, ledger, payload.get("execution_id"))
            result = _result(run_id, payload.get("execution_id")).get("result") or {}
            brief = _brief("REPAIR", f"Naprawa #{payload.get('attempt')}", meta,
                           goal="Naprawić tylko wskazane uwagi: " + ", ".join(payload.get("addresses", [])),
                           done=[result.get("summary"),
                                 ("Zmienione pliki: " + ", ".join(result.get("changed_files", [])))
                                 if result.get("changed_files") else None],
                           checks=_checks(result.get("checks")),
                           problems=[f"Niepewność: {u}" for u in result.get("uncertainties", [])],
                           handed=["→ ponowna weryfikacja i review"], at=at, evidence=[payload.get("execution_id")])
        elif kind == "ESCALATED":
            # The call whose output led to the escalation (if any), for raw evidence.
            last = next((e for e in reversed(state.get("executions", []))
                         if e.get("phase") == event.get("phase") and (e.get("iteration_id") in (iteration_id, None)
                                                                      or iteration_id is None)), {})
            meta = _exec_meta(state, ledger, last.get("execution_id"))
            brief = _brief("ESCALATED", "Zatrzymanie (eskalacja)", meta, goal=None,
                           done=[ESCALATION_TEXT.get(payload.get("code"), payload.get("code"))],
                           checks=[], problems=[f"[{payload.get('code')}] {payload.get('detail')}"],
                           handed=["→ Human Gate"], at=at, evidence=[last.get("execution_id")])
            if iteration_id is None and last.get("iteration_id"):
                iteration_id = last["iteration_id"]
        elif kind == "RUN_PAUSED":
            brief = _brief("PAUSED", "Zatrzymano bezpiecznie", {}, goal=None,
                           done=[f"Zatrzymano na granicy etapów; wznowienie od: {payload.get('resume_phase')}"],
                           checks=[], problems=[], handed=["Stan i dowody zapisane"], at=at, evidence=[])
        elif kind == "RUN_CANCELLED_IN_FLIGHT":
            effect = _exec_meta(state, ledger, payload.get("execution_id"))
            problems = []
            if payload.get("side_effect_phase"):
                problems.append("Przerwano etap zmieniający pliki — skutki w kopii roboczej NIEPEWNE; "
                                "po wznowieniu AAW nie powtórzy go automatycznie i poprosi o decyzję.")
            else:
                problems.append("Przerwano etap tylko do odczytu; po wznowieniu zostanie powtórzony "
                                "pod nowym execution_id. Praca po stronie providera mogła się dokończyć (koszt).")
            brief = _brief("CANCELLED", "Zatrzymano w trakcie kroku", effect, goal=None,
                           done=[f"Przerwany krok: {payload.get('executor')}"], checks=[], problems=problems,
                           handed=["Stan i dowody zapisane"], at=at, evidence=[payload.get("execution_id")])
        elif kind == "IN_FLIGHT_RECONCILED":
            decision = payload.get("decision")
            text = {"ADOPT_RECORDED_RESULT": "Użyto zapisanego wyniku przerwanego kroku (bez ponownego wywołania).",
                    "REPLAY_READ_ONLY_PHASE_WITH_NEW_EXECUTION_ID": "Krok tylko do odczytu zostanie powtórzony pod nowym execution_id.",
                    "ESCALATE_SIDE_EFFECT_PHASE": "Krok zmieniający pliki nie zostanie powtórzony w ciemno — decyzja człowieka.",
                    }.get(decision, decision)
            brief = _brief("RECONCILED", "Wznowienie", {"execution_id": payload.get("execution_id")}, goal=None,
                           done=[text], checks=[], problems=[], handed=[], at=at,
                           evidence=[payload.get("execution_id")])
        if brief is None:
            continue
        key = iteration_id or "RUN"
        by_iteration.setdefault(key, []).append(brief)
    return by_iteration


def timeline(state: Mapping[str, Any], briefs: Mapping[str, list[dict[str, Any]]],
             status: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    escalation = state.get("escalation") or {}
    for it in state.get("iterations", []):
        repairs = len(it.get("repairs") or [])
        if it.get("status") == "ACCEPTED":
            label = ("REPAIR → " * repairs) + "PASS"
        elif it.get("status") == "PROVISIONAL":
            # chain mode: done for planning purposes, the serious review of the whole chain decides for good
            label = ("REPAIR → " * repairs) + ("PASS (wstępnie)" if state.get("status") == ac.RUNNING
                                               else "PASS wstępnie — bez review serii")
        elif escalation.get("iteration_id") == it["iteration_id"] or it.get("status") == "ESCALATED":
            label = ("REPAIR → " * repairs) + "ESKALACJA"
        elif state.get("status") == ac.RUNNING:
            label = ("REPAIR → " * repairs) + ("IN PROGRESS" if status["status"] in (S_RUNNING, S_STOPPING)
                                               else "WSTRZYMANA")
        else:
            label = ("REPAIR → " * repairs) + "PRZERWANA"
        rows.append({"index": it.get("index"), "iteration_id": it["iteration_id"], "label": label,
                     "goal": (it.get("plan") or {}).get("goal"), "roadmap_refs": it["lineage"]["roadmap_refs"],
                     "started_at": it.get("started_at"), "finished_at": it.get("finished_at"),
                     "briefs": briefs.get(it["iteration_id"], [])})
    planning = state.get("planning") or {}
    if planning and planning.get("iteration_id") not in {r["iteration_id"] for r in rows}:
        rows.append({"index": planning.get("index"), "iteration_id": planning.get("iteration_id"),
                     "label": "PLANOWANIE" if state.get("status") == ac.RUNNING else "PRZERWANA",
                     "goal": None, "roadmap_refs": [], "started_at": planning.get("at"), "finished_at": None,
                     "briefs": briefs.get(planning.get("iteration_id"), [])})
    run_level = briefs.get("RUN") or []
    return rows + ([{"index": None, "iteration_id": None, "label": "ZDARZENIA RUNU", "goal": None,
                     "roadmap_refs": [], "briefs": run_level}] if run_level else [])


def human_gate(run_id: str, state: Mapping[str, Any], task: Mapping[str, Any],
               ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    if state.get("status") not in (ac.AWAITING_HUMAN, ac.HUMAN_APPROVED, ac.PROMOTED, ac.REJECTED):
        return None
    hold = state.get("hold") or {}
    escalation = state.get("escalation") or {}
    titles = {i["item_id"]: i["title"] for i in state["mandate"]["roadmap_mandate"]["items"]}
    roadmap = state.get("roadmap") or {}
    if hold.get("reason") == ac.HOLD_ESCALATION:
        why = ESCALATION_TEXT.get(escalation.get("code"), "AAW zatrzymał się i potrzebuje decyzji człowieka.")
    else:
        why = HOLD_TEXT.get(hold.get("reason"), hold.get("detail") or hold.get("reason"))
    done = [f"Iteracja {it['index']}: {(it.get('plan') or {}).get('goal')} — "
            f"{'REPAIR → ' * len(it.get('repairs') or [])}PASS"
            for it in state.get("iterations", []) if it.get("status") == "ACCEPTED"]
    remaining = []
    standing_end = None
    standing_open = False
    for item_id, row in roadmap.items():
        if row.get("status") == ac.R_DONE:
            continue
        if row.get("recurring"):
            # The standing "keep going" item is not a to-do point. Skipped with a reason = the planner's normal
            # end of an autonomous run; still pending = a fuse (cap/stop/escalation) interrupted a run that could
            # have continued.
            if row.get("status") == ac.R_SKIPPED:
                standing_end = row.get("reason")
            else:
                standing_open = True
            continue
        reason = row.get("reason")
        state_text = {ac.R_PENDING: "do zrobienia", ac.R_SKIPPED: "pominięty",
                      ac.R_HUMAN_REQUIRED: "wymaga człowieka"}.get(row.get("status"), row.get("status"))
        remaining.append(f"{titles.get(item_id, item_id)} — {state_text}" + (f" ({reason})" if reason else ""))
    warnings = []
    for it in state.get("iterations", []):
        for f in (it.get("final_review") or {}).get("non_blocking_findings", []):
            warnings.append(f"Uwaga z final review (it. {it['index']}): {_finding_text(f)}")
        for u in (it.get("execution") or {}).get("uncertainties", []):
            warnings.append(f"Niepewność implementacji (it. {it['index']}): {u}")
        for c in it.get("classified", []):     # not masked: a classified limitation stays visible to the human
            warnings.append(f"Sklasyfikowane ograniczenie (it. {it['index']}): {c.get('id')} — "
                            f"{c.get('classification')}: {c.get('description')} [dowód: {c.get('evidence_ref')}]")
    for f in state.get("deferred_findings", []):
        if f.get("status") == "OPEN":
            warnings.append(f"Odroczona drobna uwaga ({f.get('severity')}): {f.get('summary')}"
                            + (f" [{f.get('file')}]" if f.get("file") else ""))
    for execution_id, entry in ledger.items():
        close = (entry.get("closed") or [{}])[-1].get("payload", {}) if entry.get("closed") else {}
        if entry.get("state") in ("INTENT_ONLY", "STARTED_NOT_CLOSED"):
            warnings.append(f"Wywołanie {execution_id} nie ma zapisanego zamknięcia ({entry.get('state')}) — "
                            "jego skutek jest nieznany.")
        elif close.get("close_reason") == "CANCELLED" and close.get("effect_certainty") == "UNKNOWN":
            warnings.append(f"Wywołanie {execution_id} zostało przerwane przez STOP — skutek po stronie providera "
                            "niepewny.")
        elif close.get("close_reason") == "TIMEOUT":
            result = _result(run_id, execution_id)
            elapsed = result.get("wall_time_s")
            limit = result.get("timeout_s") or (task.get("executor_limits") or {}).get("timeout_s")
            retained = "Kompletny wynik strukturalny został zachowany." if result.get("result") else \
                       "Zachowano log procesu i zmiany w worktree; brak kompletnego wyniku strukturalnego."
            warnings.append(f"Wywołanie {execution_id} przekroczyło limit {limit}s"
                            + (f" po {elapsed}s. " if elapsed else ". ") + retained)
    alternatives = [s for s in (task.get("resolution") or {}).get("slots", {}).values()
                    if s.get("status") == "ALTERNATIVE"]
    if alternatives:
        warnings.append(f"Użyto jawnych alternatyw dla {len(alternatives)} etapów (rekomendowane profile były "
                        "niedostępne): " + "; ".join(f"{s['label']} → {s['profile_id']}" for s in alternatives))
    if escalation:
        warnings.insert(0, f"Szczegóły eskalacji [{escalation.get('code')}]: {escalation.get('detail')}")
    fingerprint = hold.get("candidate_fingerprint") or {}
    workspace = task.get("workspace") or {}
    changed_files = fingerprint.get("changed_files", [])
    if not changed_files and workspace.get("worktree"):
        try:
            changed_files = (prun.inspect_repo(str(workspace["worktree"])).get("dirty_files") or [])
        except Exception:
            changed_files = []
    decision = product_home.read_json(prun.product_dir(run_id) / "decision.json")
    awaiting = state.get("status") == ac.AWAITING_HUMAN
    return {
        "why": why, "hold_reason": hold.get("reason"), "escalation_code": escalation.get("code"),
        "done": done, "remaining": remaining, "warnings": warnings, "planner_end_reason": standing_end,
        "could_continue": standing_open,
        "candidate": {"candidate_id": hold.get("candidate_id"), "head": fingerprint.get("head"),
                      "diff_sha256": fingerprint.get("diff_sha256"),
                      "changed_files": changed_files,
                      "last_repair": hold.get("last_repair"),
                      "branch": workspace.get("branch"), "worktree": workspace.get("worktree"),
                      "base_commit": workspace.get("base_commit"), "promotable": bool(hold.get("promotable")),
                      "roadmap_exhausted": bool(hold.get("roadmap_exhausted"))},
        "actions": {"accept": awaiting and bool(hold.get("promotable")),
                    "accept_needs_early_end": awaiting and bool(hold.get("promotable")) and not hold.get("roadmap_exhausted"),
                    "add_direction": awaiting or state.get("status") in (ac.HUMAN_APPROVED, ac.PROMOTED),
                    "new_goal": awaiting or state.get("status") in (ac.HUMAN_APPROVED, ac.PROMOTED),
                    "reject": awaiting, "evidence": True},
        "decision": decision, "human": state.get("human"), "promotion": state.get("promotion"),
        "accept_meaning": ("Akceptuj = READY_FOR_EXTERNAL_INTEGRATION: wynik jest gotowy, żebyś sam go zintegrował. "
                           "AAW nie zrobi merge ani push."),
        "merge_push": "AAW nie wykonał merge ani push.",
        "main_merge_allowed": bool(state.get("main_merge_allowed", False)),
    }


def run_view(run_id: str, *, include_live: bool = True) -> dict[str, Any]:
    task = prun.load_task(run_id)
    state = _state(run_id)
    status = classify(run_id, state or {})  # one read: status and gate always describe the same state
    form = task.get("form") or {}
    base = {"run_id": run_id, "status": status["status"], "status_label": STATUS_LABEL[status["status"]],
            "section": HOME_SECTION[status["status"]], "project": (task.get("workspace") or {}).get("project_name"),
            "goal": form.get("goal"), "created_at": task.get("created_at"),
            "workspace": task.get("workspace"), "resolution": task.get("resolution"),
            "controls": {"stop": status["status"] in (S_RUNNING, S_STARTING),
                         "force_stop": status["status"] in (S_RUNNING, S_STOPPING, S_STARTING),
                         "resume": status["status"] in (S_PAUSED, S_INTERRUPTED),
                         "lock_token": status["lock"]["owner_token"]},
            "status_detail": status.get("detail"),
            "setup_notes": {"recommendation": (form.get("advanced") or {}).get("recommendation"),
                            "exploration": ((task.get("roles_config") or {}).get("exploration") or None)},
            "stop_request": status.get("stop_request"),
            "stop_effect": product_home.read_json(prun.product_dir(run_id) / "stop_effect.json"),
            "worker_exit": status.get("worker_exit")}
    if not state:
        return {**base, "process": None, "timeline": [], "gate": None, "last_activity": task.get("created_at")}
    events = _journal(run_id)
    ledger = _ledger(run_id)
    briefs = build_briefs(run_id, state, events, ledger)
    mandate_rm = state["mandate"]["roadmap_mandate"]
    direction = {"goal": form.get("goal"), "first_iteration": form.get("first_iteration"),
                 "directions_text": mandate_rm.get("direction_text") or "\n".join(mandate_rm.get("possible_directions", [])),
                 "frozen": True}
    process = process_view(run_id, state, status, events)
    live, settlement = None, None
    try:
        if include_live:
            live, settlement = _live_and_settlement(run_id, state, events)
            process["eta"] = (live["forecast"] or {}).get("estimate")
    except Exception as exc:       # the experience layer must never hide a run
        live = {"error": f"{type(exc).__name__}: {exc}"}
    gate = human_gate(run_id, state, task, ledger)
    if gate is not None and settlement is not None:
        gate["settlement"] = settlement
    return {**base, "direction": direction, "working_roadmap": state.get("working_roadmap"),
            "working_roadmap_history": state.get("working_roadmap_history", []),
            "process": process, "live": live,
            "timeline": timeline(state, briefs, status), "gate": gate,
            "last_activity": events[-1].get("occurred_at") if events else state.get("updated_at"),
            "engine": {"status": state.get("status"), "phase": state.get("phase"),
                       "policy_preset": state.get("policy_preset"), "contract": state.get("contract")}}


# ── experience (the benchmark that builds itself) ────────────────────────────

def _names(rows: list[dict[str, Any]]) -> dict[str, str]:
    import product_recommendations as pr
    return {pid: pr.profile_display(pid) for pid in {r["profile_id"] for r in rows if r.get("profile_id")}}


def _history(exclude_run: str | None = None) -> dict[str, Any]:
    data = ex.scan(prun.runs_root())
    if exclude_run:
        data["rows"] = [r for r in data["rows"] if r["run_id"] != exclude_run]
    return data


def _records(run_id: str, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    adir = prun.autonomy_dir(run_id)
    return tel.read_records(adir / "telemetry.jsonl") or tel.reconstruct_from_state(state, adir / "RESULTS", tel.load_pricing())


_LIVE_CACHE: dict[str, tuple[tuple, tuple]] = {}


def _live_and_settlement(run_id: str, state: Mapping[str, Any], events: list[dict[str, Any]]):
    """Live block + settlement, recomputed only when the run or any run's evidence changed (the page polls every 1.5 s)."""
    adir = prun.autonomy_dir(run_id)
    key = (state.get("updated_at"), state.get("status"), len(events), ex._stamp(adir / "telemetry.jsonl"),
           ex.scan_stamp(prun.runs_root()))
    hit = _LIVE_CACHE.get(run_id)
    if hit and hit[0] == key:
        return hit[1]
    result = _compute_live_and_settlement(run_id, state, events)
    _LIVE_CACHE[run_id] = (key, result)
    return result


def _compute_live_and_settlement(run_id: str, state: Mapping[str, Any], events: list[dict[str, Any]]):
    records = _records(run_id, state)
    rows = ex.rows_from_state(state, records)
    history = _history(run_id)["rows"]
    live = ex.live_view(state, events, records, rows, history)
    settlement = None
    if state.get("status") != ac.RUNNING and rows:
        everything = history + rows
        settlement = ex.settlement(rows, ex.benchmark(everything, names=_names(everything)))
    return live, settlement


def experience_view(query: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What the user's own runs say. `demo=<persona>` returns a clearly labelled synthetic preview instead."""
    query = query or {}
    scope = query.get("scope") if query.get("scope") in ("implementation", "total") else "implementation"
    demo = query.get("demo")
    if demo:
        rows = ex.demo_rows(demo if demo in ex.PERSONAS else "backend")
        names = dict(ex.DEMO_NAMES)
        bench = ex.benchmark(rows, scope=scope, names=names)
        return {"demo": True, "persona": demo, "personas": {k: v[0] for k, v in ex.PERSONAS.items()}, "scope": scope,
                "benchmark": bench, "recommendations": ex.recommendations(bench, rows),
                "horizon": ex.horizon([{"outcome": o, "iterations": n, "why": w} for o, n, w in
                                       [("FINISHED", 9, None), ("FINISHED", 6, None), ("FINISHED", 14, None),
                                        ("ESCALATED", 4, "REVIEW_ESCALATED"), ("FINISHED", 8, None), ("CAP", 40, None)]]),
                "structure": None, "runs": [], "kinds": {k: ex.KIND_LABEL[k] for k in ex.KINDS}}
    data = _history()
    rows = data["rows"]
    names = _names(rows)
    bench = ex.benchmark(rows, scope=scope, names=names)
    return {"exploration": _exploration_block(rows), "demo": False, "persona": None, "personas": {k: v[0] for k, v in ex.PERSONAS.items()}, "scope": scope,
            "benchmark": bench, "recommendations": ex.recommendations(bench, rows, data["records"], data["summaries"]),
            "horizon": ex.horizon(data["summaries"]),
            "structure": ex.cost_structure(data["records"]) if data["records"] else None,
            "runs": sorted(data["summaries"], key=lambda s: str(s.get("started_at")), reverse=True)[:12],
            "kinds": {k: ex.KIND_LABEL[k] for k in ex.KINDS}}


def _chain_length(chain_mode: Any = None) -> int | None:
    if chain_mode is False:
        return None
    import product_recommendations as pr
    cfg = pr._chain_defaults() or {}
    return int(cfg["length"]) if cfg.get("enabled") and isinstance(cfg.get("length"), int) else None


def _exploration_block(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Whether exploration is on, how many trials it has produced, and which cells it would fill."""
    try:
        import product_home as ph
        import product_recommendations as pr
        detection = prun.detection_snapshot()
        settings = ph.load_settings()
        resolution = pr.resolve_choices({g: settings.get(g) for g in pr.CHOICE_GROUPS}, runnable=prun.pp.runnable_profiles(detection),
                                        detection=detection, states=prun.pp.profile_states(detection))
        info, _ = prun.exploration_setup({"advanced": {"exploration": True}}, settings, resolution, detection, rows)
        return {**info, "setting_enabled": bool(settings.get("exploration_enabled")),
                "explored_trials": sum(1 for r in rows if r.get("explored"))}
    except Exception:
        return None


def forecast_view(body: Mapping[str, Any]) -> dict[str, Any]:
    """Before START: what the user's history says about this goal and the whole roadmap, and - if their own data
    supports it - which model to use for this kind of work (a proposal the UI applies only after confirmation)."""
    data = _history()
    names = _names(data["rows"])
    bench = ex.benchmark(data["rows"], names=names)
    directions = [str(d) for d in (body.get("directions") or [])]
    out = ex.forecast_for_goal(str(body.get("goal") or ""), directions, bench, data["rows"])
    rec = out.get("recommended")
    if rec:
        detection = prun.detection_snapshot()
        current = str(body.get("current_profile_id") or "") or None
        import product_recommendations as pr
        rec["apply"] = {"slot": "implementer_default", "profile_id": rec["profile_id"],
                        "runnable": rec["profile_id"] in prun.pp.runnable_profiles(detection),
                        "differs": rec["profile_id"] != current, "current_profile_id": current,
                        "current_label": pr.profile_display(current) if current else None, "kind": out["kind"]}
    first = str(body.get("first_iteration") or body.get("goal") or "").strip()
    items = ([{"title": first, "kind": ex.classify_task(first)["kind"]}] if first else []) + [
        {"title": prun.HUMAN_GATE.sub("", d, count=1).strip() or d, "human_required": bool(prun.HUMAN_GATE.match(d)),
         "kind": ex.classify_task(prun.HUMAN_GATE.sub("", d, count=1))["kind"]} for d in directions]
    out["roadmap"] = ex.forecast_for_roadmap(
        items, data["rows"], horizon_stats=ex.horizon(data["summaries"]), chain_length=_chain_length(body.get("chain_mode")),
        max_iterations=int(body.get("max_iterations") or ac.DEFAULT_MAX_ITERATIONS),
        continuous=bool(body.get("continuous", True))) if items else None
    return out


def intake_view(body: Mapping[str, Any]) -> dict[str, Any]:
    """Idea -> scope and roadmap with its forecast. One user-initiated call to the planner this run would use anyway."""
    import product_intake as pi
    import product_recommendations as pr
    detection = prun.detection_snapshot()
    settings = product_home.load_settings()
    choices = {g: body.get(g) or settings.get(g) for g in pr.CHOICE_GROUPS}
    resolution = pr.resolve_choices(choices, runnable=prun.pp.runnable_profiles(detection), detection=detection,
                                    states=prun.pp.profile_states(detection))
    planner = resolution["slots"]["initial_planner"]
    if planner["status"] == "UNAVAILABLE" or planner["profile_id"] not in prun.pp.runnable_profiles(detection):
        raise prun.ProductError("Model planisty nie jest teraz dostępny — wybierz inny poziom planowania w kroku „Modele”.")
    repo = None
    if body.get("repo"):
        info = prun.inspect_repo(str(body["repo"]))
        repo = info.get("top") if info.get("ready") else None
    data = _history()
    try:
        return pi.propose(str(body.get("idea") or ""), repo=repo, planner_profile_id=planner["profile_id"], history=data,
                          chain_length=_chain_length(body.get("chain_mode")), max_iterations=ac.DEFAULT_MAX_ITERATIONS,
                          continuous=True, timeout=pi.DEFAULT_TIMEOUT_S)
    except pi.IntakeError as exc:
        raise prun.ProductError(str(exc)) from exc


def home_view() -> dict[str, Any]:
    sections: dict[str, list[dict[str, Any]]] = {"running": [], "paused": [], "attention": [], "completed": []}
    for run_id in prun.list_run_ids():
        try:
            view = run_view(run_id, include_live=False)        # the Home cards need no live block
        except Exception as exc:  # one broken run must not hide the others
            sections["attention"].append({"run_id": run_id, "status": "UNREADABLE", "status_label": "Nieczytelne",
                                          "goal": str(exc)[:200]})
            continue
        process = view.get("process") or {}
        goal = view["goal"] or ""
        card = {"run_id": run_id, "project": view["project"], "goal": goal,
                "short_goal": goal if len(goal) <= 110 else goal[:107].rstrip() + "…", "status": view["status"],
                "can_resume": view["controls"]["resume"], "lock_token": view["controls"]["lock_token"],
                "status_label": view["status_label"], "iteration": process.get("iteration"),
                "phase": next((n["label"] for n in process.get("nodes", []) if n["state"] in ("active", "stopped",
                                                                                                  "failed")),
                              None) if process else None,
                "activity": process.get("activity"), "last_activity": view.get("last_activity"),
                "roadmap": process.get("roadmap")}
        sections[view["section"]].append(card)
    sections["completed"] = sections["completed"][:20]
    return {"sections": sections}


def evidence_view(run_id: str, execution_id: str | None = None) -> dict[str, Any]:
    """Raw artifacts, verbatim (bounded), for 'Pokaż surowe dowody'."""
    adir = prun.autonomy_dir(run_id)
    if execution_id:
        if not execution_id.replace("_", "").isalnum():
            raise prun.ProductError("nieprawidłowy execution_id")
        descriptor = product_home.read_json(prun.run_dir(run_id) / "EXECUTIONS" / f"{execution_id}.json")
        ledger = _ledger(run_id).get(execution_id)
        return {"execution_id": execution_id, "descriptor": descriptor, "ledger": ledger,
                "result_artifact": _result(run_id, execution_id),
                "journal_events": [e for e in _journal(run_id) if json.dumps(e.get("payload", {})).find(execution_id) >= 0]}
    state = _state(run_id) or {}
    packets = sorted((adir / "PACKETS").glob("*.diff")) if (adir / "PACKETS").is_dir() else []
    return {"run_dir": str(prun.run_dir(run_id)), "state_path": str(adir / "autonomy_state.json"),
            "journal": _journal(run_id)[-400:], "executions": state.get("executions", []),
            "mandate": state.get("mandate"), "directional_charter": state.get("directional_charter"),
            "latest_diff": packets[-1].read_text(encoding="utf-8", errors="replace")[:200_000] if packets else None,
            "task": prun.load_task(run_id)}
