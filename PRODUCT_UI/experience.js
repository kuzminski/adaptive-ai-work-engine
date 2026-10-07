"use strict";
// AAW — live run panel, the self-building benchmark ("Doświadczenie"), forecast and settlement.
// Loaded before app.js: only function declarations here; they use app.js helpers (esc, api, view, fmtDuration…) at call time.
// Every number comes from /api/runs/<id> (live block) or /api/experience — derived from the run's own telemetry.

const EXP = {kind: "ALL", scope: "implementation", demo: null, data: null};
const KIND_ORDER = ["BUGFIX", "FEATURE", "REFACTOR", "TESTS", "DOCS", "UI", "INFRA", "ANALYSIS", "OTHER"];
const CATEGORY_LABEL = {PLANNING: "Planowanie", IMPLEMENTATION: "Implementacja", VERIFICATION: "Weryfikacja", REVIEW: "Review"};
const EXECUTOR_LABEL = {plan: "planowanie", execute: "implementacja", self_verify: "weryfikacja", prepare_packet: "przygotowanie review",
                        review: "review", final_review: "review końcowe", repair: "naprawa", diagnose: "diagnoza"};
const OUTCOME_LABEL = {FINISHED: "Doszło do końca", CAP: "Bezpiecznik iteracji", ESCALATED: "Wymagało człowieka", RUNNING: "W toku"};
const CONFIDENCE_LABEL = {RELIABLE: "wiarygodne", PRELIMINARY: "wstępne", TOO_FEW: "za mało prób"};

function fmtCost(v, unit) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (unit === "USD") return v >= 1 ? `$${v.toFixed(2)}` : v >= 0.01 ? `$${v.toFixed(3)}` : `$${v.toFixed(4)}`;
  return v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k j.` : `${Math.round(v)} j.`;
}
const fmtPct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const fmtTok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));
function fmtClock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}
const colorOf = (i) => `var(--c${(i % 8) + 1})`;

// ── live elapsed time without refetching ─────────────────────────────────────
let tickerStarted = false;
function startTicker() {
  if (tickerStarted) return;
  tickerStarted = true;
  setInterval(() => {
    document.querySelectorAll("[data-since]").forEach((el) => {
      const t = Date.parse(el.dataset.since);
      if (!isNaN(t)) el.textContent = fmtDuration(Math.max(0, Math.round((Date.now() - t) / 1000)));
    });
  }, 1000);
}

// ── the run as a journey ─────────────────────────────────────────────────────
function journeyHtml(stages) {
  const mark = {done: "✓", active: "●", failed: "✕", pending: ""};
  return `<ol class="journey" aria-label="Droga od pomysłu do decyzji">${stages.map((s, i) => `<li class="stage ${esc(s.state)}">
    <span class="dot">${mark[s.state] ?? ""}${s.state === "pending" ? i + 1 : ""}</span>
    <span class="s-label">${esc(s.label)}</span><span class="s-detail">${esc(s.detail || "")}</span></li>`).join("")}</ol>`;
}
function chainHtml(c, unit) {
  if (!c) return "";
  const filled = c.slots.filter((s) => s.state !== "pending").length;
  const slot = (s) => {
    const tip = `${s.iteration ? `Iteracja ${s.iteration}: ${s.goal || ""}` : "jeszcze nie rozpoczęta"}${s.cost != null ? ` · koszt ${fmtCost(s.cost, unit)}` : ""}${s.closing ? " · tu: review całej serii" : ""}`;
    const label = {accepted: "✓", provisional: "◐", active: "●", failed: "✕", pending: ""}[s.state];
    const tipFull = tip + (s.explored ? " · wykonał inny model (eksploracja)" : "");
    return `<div class="slot ${esc(s.state)} ${s.closing ? "closing" : ""} ${s.explored ? "explored" : ""}" title="${esc(tipFull)}" role="img" aria-label="${esc(tipFull)}"><span>${label || s.position}</span>${s.closing ? `<em>review serii</em>` : ""}</div>`;
  };
  return `<div class="chain"><div class="k-label">Łańcuch ${esc(c.chain_id)} · ${filled} z ${esc(c.length)} iteracji · zamkniętych łańcuchów: ${esc(c.closed)}</div>
    <div class="slots">${c.slots.map(slot).join("")}</div>
    <div class="legend small muted"><span class="lg-item"><span class="lg accepted"></span>zaakceptowana</span><span class="lg-item"><span class="lg provisional"></span>gotowa wstępnie (czeka na review serii)</span><span class="lg-item"><span class="lg active"></span>w toku</span><span class="lg-item"><span class="lg pending"></span>przed nami</span></div></div>`;
}
function meterHtml(m) {
  const unit = m.cost_usd !== null && m.unpriced_calls === 0 ? "USD" : "proxy";
  const cost = unit === "USD" ? m.cost_usd : m.proxy_units;
  const cached = m.input_tokens ? Math.round((m.cached_tokens / m.input_tokens) * 100) : 0;
  const segs = ["PLANNING", "IMPLEMENTATION", "VERIFICATION", "REVIEW"].map((k) => ({k, v: (m.share || {})[k] || 0}));
  const total = segs.reduce((a, s) => a + s.v, 0);
  return `<div class="meter">
    <div class="stat-row">
      <div class="stat"><div class="k">Koszt dotąd</div><div class="v">${fmtCost(cost, unit)}</div></div>
      <div class="stat"><div class="k">Wywołania modeli</div><div class="v">${esc(m.calls)}${m.failed_calls ? ` <span class="small bad-t">(${esc(m.failed_calls)} nieudane)</span>` : ""}</div></div>
      <div class="stat"><div class="k">Tokeny wej. / wyj.</div><div class="v">${fmtTok(m.input_tokens)} <span class="small muted">/ ${fmtTok(m.output_tokens)}</span></div></div>
      <div class="stat"><div class="k">Z cache</div><div class="v">${cached}%</div></div>
      <div class="stat"><div class="k">Czas modeli</div><div class="v">${m.wall_s != null ? fmtDuration(Math.round(m.wall_s)) : "—"}</div></div>
    </div>
    ${total > 0 ? `<div class="k-label" style="margin-top:12px">Na co idzie koszt</div>
      <div class="sharebar" role="img" aria-label="Udział kosztu: ${segs.map((s) => `${CATEGORY_LABEL[s.k]} ${fmtPct(s.v)}`).join(", ")}">${segs.filter((s) => s.v > 0).map((s) => `<div class="seg ${s.k}" style="flex:${s.v}" title="${CATEGORY_LABEL[s.k]} ${fmtPct(s.v)}"><span>${fmtPct(s.v)}</span></div>`).join("")}</div>
      <div class="legend small muted">${segs.map((s) => `<span class="lg-item"><span class="lg ${s.k}"></span>${CATEGORY_LABEL[s.k]}</span>`).join("")}</div>` : ""}
    ${m.unpriced_calls ? `<div class="small muted" style="margin-top:6px">${esc(m.unpriced_calls)} wywołań bez ceny — koszt pokazany w jednostkach przybliżonych.</div>` : ""}</div>`;
}
function forecastHtml(f) {
  if (!f) return "";
  const e = f.estimate;
  if (!e) return `<div class="note small"><strong>Ile jeszcze?</strong> ${esc(f.note || "")}${f.pending_items ? ` Do zrobienia: ${esc(f.pending_items)} pkt roadmapy.` : ""}</div>`;
  const range = (r, fmt) => r ? `${fmt(r.low)} – ${fmt(r.high)} <span class="small muted">(typowo ${fmt(r.mid)})</span>` : "—";
  return `<div class="note small"><strong>Ile jeszcze?</strong> ${esc(e.iterations_left)} pkt roadmapy${f.open_ended ? " + kontynuacja" : ""}
    · koszt ${range(e.cost, (v) => fmtCost(v, e.unit))} · czas ${range(e.wall_s, (v) => fmtDuration(v))}
    <span class="small muted">· podstawa: ${esc(e.source.toLowerCase())}, ${esc(e.n)} iteracji</span>${f.polish_expected ? ` <span class="small muted">· na końcu polerowanie</span>` : ""}
    ${f.note ? `<div class="small muted">${esc(f.note)}</div>` : ""}</div>`;
}
function liveHtml(v) {
  const l = v.live;
  if (!l || l.error || !l.journey) return l && l.error ? `<div class="note warn small">Panel „na żywo” niedostępny: ${esc(l.error)}</div>` : "";
  startTicker();
  const running = ["RUNNING", "STOPPING"].includes(v.status);
  const unit = l.meter.cost_usd !== null && l.meter.unpriced_calls === 0 ? "USD" : "proxy";
  const now = running && l.now ? `<div class="nowline"><span class="pulse-dot"></span>W toku: <strong>${esc(EXECUTOR_LABEL[l.now.executor] || l.now.executor)}</strong>
    ${v.process && v.process.model ? ` · ${esc(v.process.model)}` : ""} · <span data-since="${esc(l.now.started_at)}">${fmtDuration(Math.max(0, Math.round((Date.now() - Date.parse(l.now.started_at)) / 1000)) || ((v.process || {}).step_elapsed_s ?? 0))}</span>
    <span class="small muted">— koszt tego wywołania poznam, gdy się zakończy</span></div>` : "";
  const feed = l.feed.length ? `<details class="feed" ${running ? "open" : ""}><summary>Przebieg na żywo (${l.feed.length})</summary><ul>${l.feed.map((f) =>
    `<li><span class="t mono">${esc(fmtClock(f.at))}</span> ${esc(f.text)}</li>`).join("")}</ul></details>` : "";
  return `<div class="panel live"><div class="live-head"><h2>Na żywo: od pomysłu do decyzji</h2>
      <span class="small muted">odświeżane co ${running ? "1,5 s" : "5 s"}</span></div>
    ${journeyHtml(l.journey)}${now}
    <div class="live-grid"><div>${chainHtml(l.chain, unit)}${l.deferred_open ? `<div class="small muted" style="margin-top:8px">Drobne uwagi odłożone na polerowanie: <strong>${esc(l.deferred_open)}</strong></div>` : ""}</div>
      <div>${meterHtml(l.meter)}</div></div>
    ${["RUNNING", "STOPPING", "PAUSED", "INTERRUPTED", "STARTING"].includes(v.status) ? forecastHtml(l.forecast) : ""}${feed}</div>`;
}

// ── settlement at the gate and forecast before START ─────────────────────────
function settlementHtml(s) {
  if (!s) return "";
  const saving = s.saving
    ? `Przy wyborze, który w Twoich danych wypada najlepiej, mogło być taniej o około <strong>${fmtCost(s.saving.low, s.unit)} – ${fmtCost(s.saving.high, s.unit)}</strong>: ${s.items.map((i) => `${esc(i.kind)}: ${esc(i.from)} → ${esc(i.to)} (−${Math.round(i.saving_ratio * 100)}%)`).join("; ")}.`
    : `Brak podstaw, by wskazać tańszą ścieżkę — za mało porównywalnych danych albo wybór był już najlepszy.`;
  return `<div class="sec"><h3>Rozliczenie</h3><div class="small">Ta praca kosztowała <strong>${fmtCost(s.actual, s.unit)}</strong> (sama implementacja i naprawy). ${saving}
    <a href="#/experience">Zobacz doświadczenie →</a></div></div>`;
}
const KIND_PL = {BUGFIX: "Błędy", FEATURE: "Nowe funkcje", REFACTOR: "Refaktoryzacja", TESTS: "Testy", DOCS: "Dokumentacja", UI: "Interfejs",
                 INFRA: "Build i konfiguracja", ANALYSIS: "Analiza i badanie", OTHER: "Inne"};
const rangeText = (r, fmt) => (r ? `${fmt(r.low)} – ${fmt(r.high)} <span class="muted">(typowo ${fmt(r.mid)})</span>` : "—");

function roadmapForecastHtml(rm) {
  if (!rm) return "";
  const t = rm.total, unit = t.unit;
  const stats = [`<strong>${esc(t.iterations)}</strong> iteracji`,
    rm.chains ? `<strong>${esc(rm.chains.count)}</strong> ${rm.chains.count === 1 ? "łańcuch" : "łańcuchy"} po ${esc(rm.chains.length)} (każdy z jednym poważnym review) + polerowanie na końcu` : "review po każdej iteracji",
    t.cost ? `koszt ${rangeText(t.cost, (v) => fmtCost(v, unit))}` : "koszt: brak danych z historii",
    t.wall_s ? `czas ${rangeText(t.wall_s, (v) => fmtDuration(v))}` : ""].filter(Boolean);
  return `<div class="rmf"><h3>Od pomysłu do końca: cała roadmapa</h3>
    <div class="small">${stats.join(" · ")}${rm.open_ended ? ` <span class="muted">· potem kontynuacja — koniec zależy od planisty</span>` : ""}</div>
    <div class="reach ${rm.reach.enough ? "known" : ""}"><span class="reach-ic">${rm.reach.enough ? Math.round(rm.reach.finished_share * 100) + "%" : "?"}</span><span class="small">${esc(rm.reach.text)}</span></div>
    ${rm.gates.length ? `<div class="small" style="margin-top:6px"><strong>Tego AAW nie zrobi sam</strong> (zostanie na liście dla Ciebie): ${rm.gates.map((g) => `„${esc(g.title)}”`).join(", ")}.</div>` : ""}
    ${rm.warnings.map((w) => `<div class="small muted" style="margin-top:4px">${esc(w)}</div>`).join("")}
    <details class="small" style="margin-top:6px"><summary>Punkty roadmapy i ich widełki</summary><table class="small"><tr><th>Punkt</th><th>Rodzaj</th><th>Koszt</th></tr>${rm.items.map((i) =>
      `<tr><td>${esc((i.title || "").slice(0, 90))}${i.human_required ? ` <span class="pill">człowiek</span>` : ""}</td><td>${esc(i.kind_label)}</td><td>${i.human_required ? `<span class="muted">—</span>` : i.cost ? rangeText(i.cost, (v) => fmtCost(v, unit)) : `<span class="muted">brak danych</span>`}</td></tr>`).join("")}</table>
      <div class="muted">${esc(rm.note)}</div></details></div>`;
}
function recommendationBoxHtml(f, p) {
  const rec = f.recommended;
  if (!rec) return "";
  const a = formState.advanced, ap = rec.apply || {};
  const byChain = ap.mode === "CHAIN";
  const applied = a.recommendation && a.recommendation.profile_id === rec.profile_id && (byChain
    ? !!formState.implementer_chain && formState.implementer_chain[0] === rec.profile_id
    : a.profile_overrides && a.profile_overrides.implementer_default === rec.profile_id);
  const evidence = `${fmtPct(rec.rate)} skuteczności, ${fmtCost(rec.cost_per_solved, f.recommended_unit)} za rozwiązane zadanie, ${esc(CONFIDENCE_LABEL[rec.confidence] || rec.confidence)} próba n=${esc(rec.n)}`;
  let action = "";
  if (applied) action = `<div class="note ok small" style="margin-top:6px">Zastosowano: domyślny model implementacji to <strong>${esc(rec.label)}</strong> (Twoja decyzja). <button class="link" id="rec-undo">Cofnij</button></div>`;
  else if (!ap.runnable) action = `<div class="small muted" style="margin-top:6px">Ten model nie jest teraz dostępny na tym komputerze, więc nie mogę go zaproponować do użycia.</div>`;
  else if (!ap.differs) action = `<div class="small muted" style="margin-top:6px">To już model, który zostanie użyty.</div>`;
  else action = `<div style="margin-top:8px"><button id="rec-apply">Użyj ${esc(rec.label)} jako domyślnego modelu implementacji</button>
      <span class="small muted">zamiast ${esc(ap.current_label || "obecnego")} · ${byChain ? "reszta obecnego łańcucha zostaje jako eskalacja · " : ""}zmiana tylko dla tego zadania · możesz cofnąć</span></div>`;
  return `<div class="small" style="margin-top:6px">Dla pracy typu <strong>${esc(f.kind_label)}</strong> najlepiej sprawdza się u Ciebie: <strong>${esc(rec.label)}</strong> — ${evidence}.</div>${action}`;
}
function explorationBoxHtml(ex) {
  if (!ex) return "";
  const on = ex.requested;
  const body = on && ex.enabled
    ? `Włączona: do <strong>${esc(ex.max_per_run)}</strong> iteracji tego zadania (co ${Math.round(100 / ex.max_percent)}. zwykła iteracja) może wykonać inny model — <strong>${ex.candidates.map((c) => esc(c.label)).join(", ")}</strong> — żeby uzupełnić Twój benchmark. Tylko zwykła złożoność, nigdy naprawy, nigdy pierwsza iteracja; model tego samego lub niższego kosztu; niedostępny model jest pomijany.`
    : on ? `Włączona, ale dziś nie ma czego zbierać: ${esc(ex.reason)}.` : `Wyłączona. Gdy ją włączysz, mały, widoczny odsetek zwykłych iteracji wykona inny, tańszy lub równie drogi model, żeby zebrać dane.`;
  return `<div class="small" style="margin-top:10px"><label class="check"><input type="checkbox" id="exp-run" ${on ? "checked" : ""}> <strong>Eksploracja w tym zadaniu</strong></label>
    <div class="muted" style="margin-left:24px">${body}</div></div>`;
}
async function loadForecastBox(p) {
  const box = document.getElementById("forecast-box");
  if (!box) return;
  const a = formState.advanced;
  const dirs = (p.roadmap || []).filter((r) => !r.recurring && r.item_id !== "STEP_1").map((r) => (r.human_required ? "[człowiek] " : "") + r.title);
  try {
    const f = await api("/api/experience/forecast", {goal: p.goal, first_iteration: p.first_iteration, directions: dirs, chain_mode: a.chain_mode !== false,
      current_profile_id: ((p.implementer_policy || {}).implementer_default || {}).profile_id, continuous: a.continue_autonomously !== false,
      current_chain: (formState.implementer_chain || (p.implementer_chain && p.implementer_chain.source !== "SLOTS" ? p.implementer_chain.steps.map((x) => x.profile_id) : [])),
      max_iterations: a.max_iterations ? Number(a.max_iterations) : null});
    f.recommended_unit = f.recommended_unit || f.unit;
    const per = f.per_iteration;
    box.innerHTML = `<h3>Prognoza z Twojej historii</h3>
      <div class="small">Rodzaj pracy: <strong>${esc(f.kind_label)}</strong> <span class="muted">(rozpoznany automatycznie z celu)</span></div>
      ${recommendationBoxHtml(f, p)}
      ${per ? `<div class="small" style="margin-top:6px">Typowa iteracja tego rodzaju kosztowała ${fmtCost(per.low, f.unit)} – ${fmtCost(per.high, f.unit)} (mediana ${fmtCost(per.mid, f.unit)}, ${esc(f.n)} dotychczasowych).</div>` : ""}
      ${f.note ? `<div class="small muted" style="margin-top:6px">${esc(f.note)}</div>` : ""}
      ${roadmapForecastHtml(f.roadmap)}
      ${explorationBoxHtml(p.exploration)}
      <div class="small muted" style="margin-top:8px">To tylko informacja — AAW niczego nie przełącza sam. <a href="#/experience">Jak to się liczy →</a></div>`;
    const wiz = document.getElementById("wiz");
    on("rec-apply", () => {
      const rec = f.recommended, ap = rec.apply;
      if (!confirm(`Użyć modelu ${rec.label} jako domyślnego modelu implementacji w tym zadaniu?\n\nDla pracy typu „${f.kind_label}” u Ciebie: ${fmtPct(rec.rate)} skuteczności, ${fmtCost(rec.cost_per_solved, f.recommended_unit)} za rozwiązane zadanie (n=${rec.n}).\nZastępuje: ${ap.current_label || "obecny wybór"}.\n\nZmiana dotyczy tylko tego zadania i można ją cofnąć. Trudniejsze iteracje nadal eskalują zgodnie z polityką.`)) return;
      formState.recUndo = {chain: formState.implementer_chain ? formState.implementer_chain.slice() : null, overrides: {...(a.profile_overrides || {})}};
      if (ap.mode === "CHAIN") {
        formState.implementer_chain = ap.chain.slice();
        ["implementer_default", "implementer_harder", "implementer_hard", "repair_default", "repair_hard", "review_pretreatment"].forEach((k) => delete (a.profile_overrides || {})[k]);
      } else {
        a.profile_overrides = {...(a.profile_overrides || {}), implementer_default: rec.profile_id};
      }
      a.recommendation = {slot: ap.slot, profile_id: rec.profile_id, kind: f.kind, previous_profile_id: ap.current_profile_id, n: rec.n, rate: rec.rate,
                          cost_per_solved: rec.cost_per_solved, chain: ap.mode === "CHAIN" ? ap.chain : null};
      stepStart(wiz);
    });
    on("rec-undo", () => {
      const undo = formState.recUndo || {chain: null, overrides: {}};
      if (a.recommendation && a.recommendation.slot === "implementer_chain") formState.implementer_chain = undo.chain;
      else { const o = {...(a.profile_overrides || {})}; delete o.implementer_default; a.profile_overrides = o; }
      a.recommendation = null; formState.recUndo = null;
      stepStart(wiz);
    });
    const ex = document.getElementById("exp-run");
    if (ex) ex.onchange = () => { a.exploration = ex.checked; stepStart(wiz); };
  } catch (e) { box.innerHTML = ""; }
}

// ── idea intake: a loose idea becomes scope and roadmap, with its horizon and cost, before START ──
function intakeResultHtml(r) {
  const p = r.proposal, fc = r.forecast, unit = fc.total.unit;
  const list = (items) => items.length ? `<ul class="small">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="muted small">—</div>`;
  const sev = {LOW: "niskie", MEDIUM: "średnie", HIGH: "wysokie"};
  return `<div class="intake-res">
    <h3>${esc(p.title)}</h3>
    <div class="k-label">Cel</div><div class="longtext small">${esc(p.goal)}</div>
    <div class="k-label">Pierwsza iteracja (najmniejszy działający kawałek)</div><div class="longtext small">${esc(p.first_iteration || "—")}</div>
    ${roadmapForecastHtml(fc)}
    <div class="k-label">Roadmapa (${p.roadmap.length} punktów po pierwszej iteracji)</div>
    <table class="small"><tr><th>#</th><th>Punkt</th><th>Rodzaj</th><th>Rozmiar</th></tr>${p.roadmap.map((i, n) => `<tr><td>${n + 2}</td>
      <td>${esc(i.title)}${i.why ? `<div class="muted">${esc(i.why)}</div>` : ""}${i.human_required ? `<div class="note warn small" style="margin:4px 0 0">Wymaga człowieka: ${esc(i.human_reason || "")}</div>` : ""}</td>
      <td>${esc(KIND_PL[i.kind] || i.kind)}</td><td>${esc(i.size)}</td></tr>`).join("")}</table>
    ${p.open_questions.length ? `<div class="note warn small"><strong>Pytania, które mogą zmienić zakres</strong> — odpowiedz na nie w opisie pomysłu i rozpisz ponownie, jeśli zmieniają plan:${list(p.open_questions)}</div>` : ""}
    <div class="grid2" style="margin-top:8px"><div><div class="k-label">Założenia, które przyjął planista</div>${list(p.assumptions)}</div>
      <div><div class="k-label">Ryzyka</div>${p.risks.length ? `<ul class="small">${p.risks.map((x) => `<li><strong>${esc(sev[x.severity] || x.severity)}</strong>: ${esc(x.text)}</li>`).join("")}</ul>` : `<div class="muted small">—</div>`}</div></div>
    <div class="k-label">Kryteria akceptacji</div>${list(p.acceptance_criteria)}
    ${p.done_definition ? `<div class="k-label">Kiedy uznać za gotowe</div><div class="small">${esc(p.done_definition)}</div>` : ""}
    <div class="small muted" style="margin-top:8px">Rozpisanie kosztowało: ${r.cost.cost_usd != null ? fmtCost(r.cost.cost_usd, "USD") : fmtCost(r.cost.proxy_units || 0, "proxy")}
      (${fmtTok((r.cost.tokens || {}).input_total || 0)} tokenów wej. / ${fmtTok((r.cost.tokens || {}).output || 0)} wyj.) · model: ${esc((r.planner || {}).model || (r.planner || {}).profile_id || "—")}.</div>
    <div class="actions"><button class="primary" id="intake-apply">Użyj w kreatorze</button><button id="intake-close">Odrzuć rozpis</button>
      <span class="small muted">Wypełni pola Cel, Pierwsza iteracja, Kierunek i kryteria — możesz je dalej edytować. Nic nie startuje.</span></div></div>`;
}
function intakeHtml() {
  const st = formState.intake || {};
  const planner = ((formState.setup || {}).groups || {}).planning;
  return `<details class="panel intake" id="intake" ${st.result || st.idea ? "open" : ""}><summary><strong>Masz tylko luźny pomysł?</strong>
      <span class="small muted">Najsilniejszy model rozpisze go na zakres i roadmapę i pokaże, jak daleko i za ile da się dojść — zanim cokolwiek wystartuje.</span></summary>
    <p class="hint">Opisz pomysł własnymi słowami (nawet kilka zdań). To <strong>jedno wywołanie modelu planisty</strong> (${esc(((planner || {}).models || ["najsilniejszy model"])[0])}) w trybie tylko do odczytu; zużywa część limitu.
      Otrzymasz propozycję do przejrzenia — nic nie jest zamrożone ani uruchamiane, dopóki sam nie naciśniesz START.</p>
    <textarea id="idea" rows="5" placeholder="np. Chcę prostą aplikację do dzielenia rachunków ze znajomymi: dodaję wydatki, a ona liczy, kto komu ile oddaje.">${esc(st.idea || "")}</textarea>
    <div class="actions"><button class="primary" id="intake-go">Rozpisz pomysł</button><span id="intake-state" class="small muted"></span></div>
    <div id="intake-result">${st.result ? intakeResultHtml(st.result) : ""}</div></details>`;
}
function bindIntake() {
  const f = formState;
  f.intake = f.intake || {};
  const area = document.getElementById("idea");
  if (!area) return;
  area.oninput = () => { f.intake.idea = area.value; };
  const bindResult = () => {
    on("intake-close", () => { f.intake.result = null; document.getElementById("intake-result").innerHTML = ""; });
    on("intake-apply", () => {
      const r = f.intake.result, pf = r.prefill;
      if ((f.goal || f.first_iteration || f.directions) && !confirm("Zastąpić obecną treść pól Cel / Pierwsza iteracja / Kierunek rozpisem?")) return;
      f.goal = pf.goal; f.first_iteration = pf.first_iteration; f.directions = pf.directions;
      Object.assign(f.advanced, pf.advanced);
      f.intake.applied = r.intake_id;
      draftSave(); toast("Wczytano rozpis — przejrzyj pola i edytuj do woli. Prognoza całej roadmapy będzie w podsumowaniu.", 7000);
      renderWizard();
    });
  };
  bindResult();
  on("intake-go", async (e) => {
    const idea = area.value.trim();
    if (idea.length < 12) { toast("Opisz pomysł choć w kilku zdaniach."); return; }
    const model = (((formState.setup || {}).groups || {}).planning || {}).models;
    if (!confirm(`Rozpisać pomysł?\n\nTo jednorazowo wywoła model planisty (${(model || ["najsilniejszy model"])[0]}) w trybie tylko do odczytu i zużyje część limitu. Zwykle trwa od kilkudziesięciu sekund do kilku minut.\n\nNic nie wystartuje.`)) return;
    f.intake.idea = idea;
    e.target.disabled = true;
    const state = document.getElementById("intake-state");
    state.textContent = "Rozpisuję… (nie zamykaj tej karty)";
    try {
      f.intake.result = await api("/api/intake/propose", {idea, repo: f.repo, planning: f.planning, implementation: f.implementation, review: f.review,
        chain_mode: f.advanced.chain_mode !== false});
      document.getElementById("intake-result").innerHTML = intakeResultHtml(f.intake.result);
      state.textContent = "";
      bindResult();
    } catch (err) { state.textContent = ""; toast(err.message, 9000); }
    e.target.disabled = false;
  });
}

// ── exploration card on the benchmark page ───────────────────────────────────
function explorationCardHtml(x, demo) {
  if (demo || !x) return "";
  const cells = (x.thin_cells || []).slice(0, 6).map((c) => `<li>${esc(KIND_PL[c.kind] || c.kind)}: ${esc(c.label)} — ${esc(c.n)} z 3 prób</li>`).join("");
  return `<div class="panel"><h2>Eksploracja (opcjonalna)</h2>
    <div class="small">${x.setting_enabled ? `Włączona w ustawieniach: do ${esc(x.max_per_run)} iteracji na zadanie.` : `Wyłączona. Włącz ją w <a href="#/settings">Ustawieniach</a>, jeśli chcesz, by nowe modele zbierały dane przy prawdziwej pracy.`}
      Zebrano dzięki niej dotąd: <strong>${esc(x.explored_trials)}</strong> prób.</div>
    ${cells ? `<div class="k-label" style="margin-top:8px">Komórki benchmarku, które wypełniłaby</div><ul class="small">${cells}</ul>` : `<div class="muted small" style="margin-top:6px">${esc(x.reason || "Nie ma dziś czego uzupełniać.")}</div>`}
    <div class="small muted">Zasady: tylko zwykłe iteracje, nigdy pierwsza ani naprawy, model tego samego lub niższego kosztu, twardy limit na zadanie.</div></div>`;
}

// ── the benchmark ("Doświadczenie") ──────────────────────────────────────────
function niceTicks(lo, hi, log) {
  if (log) {
    const out = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= lo * 0.9 && v <= hi * 1.1) out.push(v); }
    return out;
  }
  const step = 10 ** Math.floor(Math.log10(hi / 4)) * ([1, 2, 5, 10].find((m) => hi / (m * 10 ** Math.floor(Math.log10(hi / 4))) <= 5) || 10);
  const out = [];
  for (let v = 0; v <= hi * 1.001; v += step) out.push(v);
  return out;
}
function scatterSvg(points, unit, opts = {}) {
  const W = 780, H = 440, L = 70, R = 34, T = 26, B = 62, pw = W - L - R, ph = H - T - B;
  const xs = points.filter((p) => p.x != null && p.x > 0).map((p) => p.x);
  if (!points.length) return `<div class="empty-chart">Brak danych do narysowania.</div>`;
  const lo = xs.length ? Math.min(...xs) : 1, hi = xs.length ? Math.max(...xs) : 1;
  const log = xs.length > 1 && hi / lo > 6;
  const dLo = log ? lo / 1.6 : 0, dHi = log ? hi * 1.6 : (hi || 1) * 1.15;
  const sx = (v) => L + (log ? (Math.log10(v) - Math.log10(dLo)) / (Math.log10(dHi) - Math.log10(dLo)) : (v - dLo) / (dHi - dLo)) * pw;
  const sy = (v) => T + (1 - v) * ph;
  const railX = L + pw + 14;
  const grid = niceTicks(dLo || lo, dHi, log).filter((v) => v >= dLo * 0.999 && v <= dHi * 1.001).map((v) =>
    `<line class="grid" x1="${sx(v)}" y1="${T}" x2="${sx(v)}" y2="${T + ph}"/><text class="tick" x="${sx(v)}" y="${T + ph + 18}" text-anchor="middle">${esc(fmtCost(v, unit))}</text>`).join("");
  const ygrid = [0, .25, .5, .75, 1].map((v) => `<line class="grid" x1="${L}" y1="${sy(v)}" x2="${L + pw}" y2="${sy(v)}"/><text class="tick" x="${L - 10}" y="${sy(v) + 4}" text-anchor="end">${Math.round(v * 100)}%</text>`).join("");
  const front = points.filter((p) => p.pareto && p.x != null).sort((a, b) => a.x - b.x);
  const line = front.length > 1 ? `<polyline class="front" points="${front.map((p) => `${sx(p.x)},${sy(p.y)}`).join(" ")}"/>` : "";
  // labels: place the most important first, nudge the rest up/down so they never print on top of each other
  const placed = [], labelY = {};
  const geo = points.map((p) => ({cx: p.x != null ? sx(p.x) : railX, cy: sy(p.y), r: Math.min(24, 7 + 3.2 * Math.sqrt(p.n || 1))}));
  points.map((p, i) => i).sort((a, b) => (points[b].best ? 1e6 : points[b].n) - (points[a].best ? 1e6 : points[a].n)).forEach((i) => {
    const p = points[i], g = geo[i], text = `${p.short || p.label}${p.best ? " ★" : ""}`, w = text.length * 6.8 + 10;
    const left = g.cx > L + pw * 0.72, x0 = left ? g.cx - g.r - 8 - w : g.cx + g.r + 8;
    for (const dy of [0, -17, 17, -34, 34, -51, 51, -68, 68]) {
      const y = Math.min(T + ph - 4, Math.max(T + 12, g.cy + 4 + dy)), box = [x0, y - 13, x0 + w, y + 4];
      if (!placed.some((b) => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]))) { placed.push(box); labelY[i] = y; return; }
    }
    labelY[i] = g.cy + 4;
  });
  const dots = points.map((p, i) => {
    const {cx, cy, r} = geo[i];
    const col = colorOf(p.color), thin = p.confidence === "TOO_FEW";
    const whisk = p.ci ? `<line class="whisker" x1="${cx}" y1="${sy(p.ci[0])}" x2="${cx}" y2="${sy(p.ci[1])}" stroke="${col}"/>` : "";
    const left = cx > L + pw * 0.72;
    return `<g class="pt ${p.best ? "best" : ""} ${thin ? "thin" : ""}" tabindex="0" data-i="${i}" role="img" aria-label="${esc(p.label)}: skuteczność ${fmtPct(p.y)}, koszt ${p.x != null ? fmtCost(p.x, unit) : "brak rozwiązań"}, n=${esc(p.n)}">
      ${whisk}${p.best ? `<circle class="halo" cx="${cx}" cy="${cy}" r="${r + 5}"/>` : ""}
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="${thin ? "none" : col}" fill-opacity=".82" stroke="${col}" stroke-width="2.5" ${thin ? 'stroke-dasharray="4 3"' : ""}/>
      ${p.x == null ? `<text x="${cx}" y="${cy + 4}" text-anchor="middle" class="x-mark">✕</text>` : ""}
      <text class="plabel" x="${cx + (left ? -r - 8 : r + 8)}" y="${labelY[i]}" text-anchor="${left ? "end" : "start"}">${esc(p.short || p.label)}${p.best ? " ★" : ""}</text></g>`;
  }).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="group" aria-label="Wykres: koszt za rozwiązane zadanie a skuteczność">
    ${grid}${ygrid}
    <rect class="sweet" x="${L}" y="${T}" width="${pw * 0.42}" height="${ph * 0.3}" rx="8"/>
    <text class="hint-t" x="${L + 12}" y="${T + 20}">↖ taniej i skuteczniej</text>
    <line class="axis" x1="${L}" y1="${T + ph}" x2="${L + pw}" y2="${T + ph}"/><line class="axis" x1="${L}" y1="${T}" x2="${L}" y2="${T + ph}"/>
    <text class="axis-t" x="${L + pw / 2}" y="${H - 14}" text-anchor="middle">${esc(opts.xTitle || "Koszt za rozwiązane zadanie")}${log ? " (skala logarytmiczna)" : ""} →</text>
    <text class="axis-t" transform="translate(16 ${T + ph / 2}) rotate(-90)" text-anchor="middle">Skuteczność →</text>
    ${points.some((p) => p.x == null) ? `<text class="hint-t" x="${railX}" y="${T + ph + 18}" text-anchor="middle">bez rozwiązań</text>` : ""}
    ${line}${dots}</svg>`;
}
function chartPoints(data) {
  const b = data.benchmark;
  if (EXP.kind === "ALL") {
    const profiles = [...new Set(Object.values(b.kinds).flatMap((k) => k.points.map((p) => p.profile_id)))].sort();
    return {unit: b.unit, xTitle: "Oczekiwany koszt za rozwiązane zadanie przy Twoim miksie pracy",
            points: b.overall.map((o) => ({key: o.profile_id, label: o.label, short: o.label, x: o.cost_per_solved, y: o.rate, n: Math.max(2, Math.round(o.coverage * 10)),
              ci: null, best: !!o.is_best, pareto: true, confidence: "PRELIMINARY", color: profiles.indexOf(o.profile_id),
              extra: `pokrycie miksu: ${fmtPct(o.coverage)}`})), profiles};
  }
  const k = b.kinds[EXP.kind];
  const profiles = [...new Set(Object.values(b.kinds).flatMap((kk) => kk.points.map((p) => p.profile_id)))].sort();
  if (!k) return {unit: b.unit, points: [], profiles};
  return {unit: b.unit, xTitle: "Koszt za rozwiązane zadanie", profiles,
          points: k.points.map((p) => ({key: p.profile_id, label: p.label, short: p.label, x: p.cost_per_solved, y: p.rate || 0, n: p.n, ci: p.rate_ci95,
            best: p.is_best, pareto: p.on_pareto, confidence: p.confidence, color: profiles.indexOf(p.profile_id),
            extra: `${p.solved} z ${p.n} rozwiązanych · przedział 95%: ${fmtPct(p.rate_ci95[0])}–${fmtPct(p.rate_ci95[1])} · średni czas ${p.wall_mean_s != null ? fmtDuration(Math.round(p.wall_mean_s)) : "—"}`}))};
}
function recommendationsHtml(recs) {
  if (!recs.length) return `<div class="muted small">Za mało danych na rekomendacje. Zbieram je sam przy każdym zadaniu — nic nie musisz uruchamiać.</div>`;
  const icon = {SWITCH: "⇄", KEEP: "✓", COLLECT: "…", REVIEW_SHARE: "!", LONGER_CHAINS: "↔", SHORTER_CHAINS: "↔"};
  return `<ul class="recs">${recs.map((r) => `<li class="rec ${esc(r.type)}"><span class="ri-ic">${icon[r.type] || "•"}</span><div>${esc(r.text)}</div></li>`).join("")}</ul>`;
}
function mixHtml(mix) {
  const entries = Object.entries(mix || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return `<div class="muted small">Pojawi się po pierwszych ukończonych iteracjach.</div>`;
  return `<div class="sharebar mixbar">${entries.map(([k, v]) => `<div class="seg k-${esc(k)}" style="flex:${v}" title="${esc(EXP.data.kinds[k])} ${fmtPct(v)}"><span>${v >= 0.1 ? fmtPct(v) : ""}</span></div>`).join("")}</div>
    <div class="legend small muted">${entries.map(([k, v]) => `<span class="lg-item"><span class="lg k-${esc(k)}"></span>${esc(EXP.data.kinds[k])} ${fmtPct(v)}</span>`).join("")}</div>
    <div class="small muted" style="margin-top:6px">To Twój profil pracy: według niego liczony jest ogólny wybór. Inna praca — inny benchmark.</div>`;
}
function horizonHtml(h) {
  if (!h.enough) return `<div class="muted small">${esc(h.text)}</div>`;
  const total = h.runs, seg = (k, cls) => (h.outcomes[k] ? `<div class="seg ${cls}" style="flex:${h.outcomes[k]}" title="${OUTCOME_LABEL[k]}: ${h.outcomes[k]}"><span>${h.outcomes[k]}</span></div>` : "");
  return `<div class="sharebar">${seg("FINISHED", "ok")}${seg("CAP", "warn")}${seg("ESCALATED", "bad")}</div>
    <div class="legend small muted"><span class="lg-item"><span class="lg ok"></span>doszło do końca</span><span class="lg-item"><span class="lg warn"></span>bezpiecznik</span><span class="lg-item"><span class="lg bad"></span>wymagało człowieka</span></div>
    <div class="small" style="margin-top:8px">${esc(h.text)}</div>
    <div class="small muted" style="margin-top:4px">Iteracje na zadanie: mediana ${esc(h.iterations.median)}, środkowe 50%: ${esc(Math.round(h.iterations.p25))}–${esc(Math.round(h.iterations.p75))}.
    Dojście do końca wg wielkości zadania: ${Object.entries(h.by_size).filter(([, s]) => s.runs).map(([k, s]) => `${esc(k)} it.: ${fmtPct(s.finished_share)} (${s.runs})`).join(" · ")}.</div>`;
}
function structureHtml(s) {
  if (!s) return `<div class="muted small">Pojawi się po pierwszym zadaniu.</div>`;
  const share = s.share_of_proxy_units || {};
  return ["PLANNING", "IMPLEMENTATION", "VERIFICATION", "REVIEW"].map((k) => `<div class="bar-row"><span class="bl">${CATEGORY_LABEL[k]}</span>
    <div class="bar"><div class="fill ${k}" style="width:${Math.round((share[k] || 0) * 100)}%"></div></div><span class="bv">${fmtPct(share[k] || 0)}</span></div>`).join("") +
    `<div class="small muted" style="margin-top:6px">Udział w kosztach (jednostki przybliżone, ${esc(s.calls)} wywołań).</div>`;
}
function runsHtml(runs) {
  if (!runs.length) return `<div class="muted small">Brak zadań.</div>`;
  return `<table class="small"><tr><th>Zadanie</th><th>Wynik</th><th>Iteracje</th><th>Koszt</th></tr>${runs.map((r) => `<tr>
    <td><a href="#/runs/${esc(r.run_id)}">${esc((r.goal || r.run_id).slice(0, 70))}</a></td><td>${esc(OUTCOME_LABEL[r.outcome] || r.outcome)}</td>
    <td>${esc(r.accepted)}/${esc(r.iterations)}</td><td>${r.cost_usd != null ? fmtCost(r.cost_usd, "USD") : fmtCost(r.proxy_units, "proxy")}</td></tr>`).join("")}</table>`;
}
async function renderExperience() {
  const q = new URLSearchParams();
  if (EXP.demo) q.set("demo", EXP.demo);
  q.set("scope", EXP.scope);
  const data = await api(`/api/experience?${q}`);
  EXP.data = data;
  const b = data.benchmark;
  const kindsWithData = KIND_ORDER.filter((k) => b.kinds[k]);
  if (EXP.kind !== "ALL" && !b.kinds[EXP.kind]) EXP.kind = "ALL";
  let cp = chartPoints(data), fallbackNote = "";
  if (EXP.kind === "ALL" && !cp.points.length && kindsWithData.length) {
    const top = kindsWithData.reduce((m, k) => (b.kinds[k].n > b.kinds[m].n ? k : m), kindsWithData[0]);
    EXP.kind = top; cp = chartPoints(data);
    fallbackNote = `<div class="note small">Widok „Wszystkie” pojawi się, gdy jakiś model będzie miał co najmniej 3 próby w rodzajach pracy pokrywających połowę Twojego miksu.
      Do tego czasu pokazuję rodzaj z największą liczbą prób: <strong>${esc(data.kinds[top])}</strong>.</div>`;
  }
  const profiles = cp.profiles;
  const tabs = [`<button class="tab ${EXP.kind === "ALL" ? "on" : ""}" data-kind="ALL">Wszystkie (Twój miks)</button>`].concat(kindsWithData.map((k) =>
    `<button class="tab ${EXP.kind === k ? "on" : ""}" data-kind="${k}">${esc(data.kinds[k])} <span class="n">${b.kinds[k].n}</span></button>`)).join("");
  const empty = !kindsWithData.length;
  view.innerHTML = `<h1>Doświadczenie</h1>
    <p class="lead">Benchmark buduje się sam z Twoich zadań — niczego nie uruchamiasz i niczego nie konfigurujesz. Każde ukończone zadanie dokłada punkt.</p>
    ${data.demo ? `<div class="note warn"><strong>PODGLĄD NA DANYCH PRZYKŁADOWYCH</strong> — to nie są Twoje wyniki, tylko wymyślone dane pokazujące, jak będzie wyglądać ten ekran.
      Profil pracy: <select id="exp-persona">${Object.entries(data.personas).map(([k, n]) => `<option value="${k}" ${data.persona === k ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
      <button id="exp-demo-off" class="ghost">Wróć do moich danych</button></div>` : ""}
    <div class="exp-controls"><div class="tabs" role="tablist">${tabs}</div>
      <div class="inline" style="gap:12px"><label class="small">Koszt: <select id="exp-scope"><option value="implementation" ${EXP.scope === "implementation" ? "selected" : ""}>sama implementacja i naprawy</option>
        <option value="total" ${EXP.scope === "total" ? "selected" : ""}>całość (z review i planowaniem)</option></select></label>
        ${data.demo ? "" : `<button id="exp-demo-on" class="ghost">Zobacz przykład</button>`}</div></div>
    ${empty ? `<div class="panel empty-state"><h2>Jeszcze nie ma z czego liczyć</h2><p>Po pierwszych ukończonych iteracjach pojawią się tu punkty: każdy model, który wykonał u Ciebie pracę danego rodzaju
      (błędy, nowe funkcje, refaktoryzacja, testy, dokumentacja, interfejs…), z jego skutecznością i kosztem za rozwiązane zadanie.</p>
      <p class="muted">Rodzaj pracy rozpoznaję automatycznie z celu iteracji. Dla każdego rodzaju powstaje osobny wykres, a wybór „ogólny" ważę Twoim własnym miksem pracy — dlatego inni użytkownicy zobaczą inne wyniki.</p>
      <button id="exp-demo-on2" class="primary">Zobacz, jak to będzie wyglądać</button></div>` : `
    <div class="panel chart-panel">${fallbackNote}<div class="chart-wrap" id="chart-wrap">${scatterSvg(cp.points, cp.unit, {xTitle: cp.xTitle})}<div class="chart-tip" id="chart-tip" hidden></div></div>
      <div class="legend chart-legend small">${profiles.map((p, i) => {
        const label = (Object.values(b.kinds).flatMap((k) => k.points).find((x) => x.profile_id === p) || {}).label || p;
        return `<span class="lg-item"><span class="lg-dot" style="background:${colorOf(i)}"></span>${esc(label)}</span>`; }).join("")}
        <span class="lg-item muted">rozmiar = liczba prób · ★ = najlepszy wybór · przerywany = za mało prób · pionowa kreska = przedział 95%</span></div>
      ${b.warnings.map((w) => `<div class="small muted" style="margin-top:6px">${esc(w)}</div>`).join("")}</div>`}
    <div class="grid2">
      <div class="panel"><h2>Co byłoby optymalne</h2>${recommendationsHtml(data.recommendations)}</div>
      <div class="panel"><h2>Twój miks pracy</h2>${mixHtml(b.mix)}</div>
      <div class="panel"><h2>Jak daleko dochodzi bez człowieka</h2>${horizonHtml(data.horizon)}</div>
      <div class="panel"><h2>Na co idzie koszt</h2>${structureHtml(data.structure)}</div>
    </div>
    ${explorationCardHtml(data.exploration, data.demo)}
    ${data.runs.length ? `<div class="panel"><h2>Ostatnie zadania</h2>${runsHtml(data.runs)}</div>` : ""}
    <details class="panel"><summary>Jak to jest liczone</summary><div class="small" style="margin-top:8px">
      Dane: telemetria każdego wywołania modelu (tokeny, czas, koszt) i wynik każdej iteracji — z plików, które AAW i tak zapisuje. Nic nie opuszcza tego komputera.
      „Rozwiązane" = iteracja zaakceptowana przez review. Praca wciąż otwarta albo zatwierdzona tylko wstępnie, której seria nie została zamknięta, nie liczy się ani jako sukces, ani jako porażka.
      Koszt za rozwiązane zadanie = cały wydatek ÷ liczba rozwiązanych (więc poprawki i nieudane próby drożeją model). Porównuję modele dopiero od ${3} prób; „wiarygodne" od 8.
      Rekomenduję zmianę modelu tylko wtedy, gdy jest o co najmniej 10% tańszy przy porównywalnej skuteczności. Ceny to niezweryfikowane ceny katalogowe API — abonament rozlicza limit, nie tokeny.</div></details>`;
  const tip = document.getElementById("chart-tip");
  const wrap = document.getElementById("chart-wrap");
  view.querySelectorAll(".pt").forEach((g) => {
    const p = cp.points[Number(g.dataset.i)];
    const show = (ev) => {
      tip.hidden = false;
      tip.innerHTML = `<strong>${esc(p.label)}</strong>${p.best ? " ★ najlepszy wybór" : ""}<br>skuteczność ${fmtPct(p.y)} · koszt za rozwiązane ${p.x != null ? fmtCost(p.x, cp.unit) : "— (brak rozwiązań)"}<br>
        <span class="muted">n=${esc(p.n)} · ${esc(CONFIDENCE_LABEL[p.confidence] || p.confidence)}<br>${esc(p.extra || "")}</span>`;
      const r = wrap.getBoundingClientRect();
      const bx = ev.clientX !== undefined && ev.type !== "focus" ? ev.clientX - r.left : g.getBoundingClientRect().left - r.left + 20;
      const by = ev.clientY !== undefined && ev.type !== "focus" ? ev.clientY - r.top : g.getBoundingClientRect().top - r.top;
      tip.style.left = `${Math.min(bx + 14, r.width - 270)}px`; tip.style.top = `${Math.max(by - 10, 0)}px`;
    };
    g.addEventListener("mousemove", show); g.addEventListener("focus", show);
    g.addEventListener("mouseleave", () => (tip.hidden = true)); g.addEventListener("blur", () => (tip.hidden = true));
  });
  view.querySelectorAll(".tab").forEach((t) => (t.onclick = () => { EXP.kind = t.dataset.kind; renderExperience(); }));
  const scope = document.getElementById("exp-scope");
  if (scope) scope.onchange = () => { EXP.scope = scope.value; renderExperience(); };
  const demoOn = () => { EXP.demo = "backend"; EXP.kind = "ALL"; renderExperience(); };
  on("exp-demo-on", demoOn); on("exp-demo-on2", demoOn);
  on("exp-demo-off", () => { EXP.demo = null; EXP.kind = "ALL"; renderExperience(); });
  const persona = document.getElementById("exp-persona");
  if (persona) persona.onchange = () => { EXP.demo = persona.value; EXP.kind = "ALL"; renderExperience(); };
  if (!EXP.demo) setPoll(() => renderExperience().catch(() => {}), 15000);
}
