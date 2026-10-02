"use strict";
// AAW Product MVP V0.1 — single-page UI. Every number and status shown here
// comes from the local API, which projects the engine's own artifacts.

const TOKEN = document.querySelector('meta[name="aaw-token"]').content;
const view = document.getElementById("view");
let boot = null;          // /api/bootstrap
let pollTimer = null;
let formState = null;     // New Task form (kept while navigating)
let openIterations = new Set();

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

async function api(path, body) {
  const opts = {headers: {"X-AAW-Token": TOKEN}};
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({error: "nieprawidłowa odpowiedź"}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg, ms = 4200) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

function modal(title, html) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = html;
  document.getElementById("modal").hidden = false;
}
document.getElementById("modal-close").onclick = () => (document.getElementById("modal").hidden = true);
document.getElementById("modal").addEventListener("click", (e) => { if (e.target.id === "modal") e.target.hidden = true; });

function fmtDuration(s) {
  if (s === null || s === undefined) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min ${sec} s` : `${sec} s`;
}
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}
function pill(status, label) { return `<span class="pill ${esc(status)}">${esc(label || status)}</span>`; }

function setPoll(fn, ms) {
  clearInterval(pollTimer);
  pollTimer = fn ? setInterval(fn, ms) : null;
}

// ── router ───────────────────────────────────────────────────────────────────
async function route() {
  const hash = location.hash || "#/home";
  const [, page, arg] = hash.split("/");
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === (page === "runs" && arg ? "runs" : page)));
  setPoll(null);
  try {
    if (page === "new") return renderNew();
    if (page === "runs" && arg) return renderRun(arg);
    if (page === "runs") return renderRuns();
    if (page === "settings") return renderSettings();
    return renderHome();
  } catch (e) {
    view.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// ── home ─────────────────────────────────────────────────────────────────────
function cardHtml(c) {
  const road = c.roadmap ? `Roadmap ${c.roadmap.done} / ${c.roadmap.total}` : "";
  return `<div class="card" data-run="${esc(c.run_id)}">
    <div class="row"><span>${esc(c.project || "")}</span>${pill(c.status, c.status_label)}</div>
    <div class="goal">${esc(c.goal)}</div>
    <div class="row"><span>${c.iteration ? "Iteracja " + esc(c.iteration) : ""}${c.phase ? " · " + esc(c.phase) : ""}</span><span>${esc(road)}</span></div>
    <div class="row"><span>${esc(c.activity || "")}</span><span>${esc(fmtTime(c.last_activity))}</span></div>
  </div>`;
}
function bindCards() {
  view.querySelectorAll("[data-run]").forEach((el) => (el.onclick = () => (location.hash = `#/runs/${el.dataset.run}`)));
}
function providerBanner() {
  const det = boot.providers;
  if (!det) return `<div class="note warn">Nie sprawdzono jeszcze dostępnych CLI AI. <button class="link" id="go-detect">Wykryj teraz</button></div>`;
  if (!det.any_found) return `<div class="note bad">Nie znaleziono żadnego obsługiwanego CLI AI (Claude CLI / Codex CLI). <a href="#/settings">Zobacz, jak je zainstalować</a>.</div>`;
  const rows = det.providers.filter((p) => p.status === "FOUND").map((p) => `${esc(p.display_name)} ${esc(p.version || "")} — ${p.login === "LOGGED_IN" ? "zalogowano" : p.login === "NOT_LOGGED_IN" ? "NIEZALOGOWANO" : "logowanie nieznane"}`);
  return `<div class="note small">Dostępne: ${rows.join(" · ")} · <a href="#/settings">Ustawienia</a></div>`;
}
async function renderHome() {
  const draw = async () => {
    const data = await api("/api/home");
    const s = data.sections;
    const sec = (title, list, empty) => `<h2>${title} <span class="muted small">(${list.length})</span></h2>` +
      (list.length ? `<div class="grid">${list.map(cardHtml).join("")}</div>` : `<div class="section-empty">${empty}</div>`);
    view.innerHTML = `<h1>Home</h1><p class="lead">Co dzieje się teraz i co czeka na Ciebie.</p>
      ${providerBanner()}
      <div class="actions" style="margin:0 0 6px"><button class="primary" onclick="location.hash='#/new'">+ Nowe zadanie</button></div>
      ${sec("Wymaga uwagi", s.attention, "Nic nie czeka na Twoją decyzję.")}
      ${sec("W toku", s.running, "Nic teraz nie pracuje.")}
      ${sec("Wstrzymane", s.paused, "Brak wstrzymanych zadań.")}
      ${sec("Ostatnio zakończone", s.completed, "Brak zakończonych zadań.")}`;
    bindCards();
    const d = document.getElementById("go-detect");
    if (d) d.onclick = detectNow;
  };
  await draw();
  setPoll(() => draw().catch(() => {}), 3000);
}
async function detectNow() {
  toast("Wykrywanie CLI…");
  boot.providers = await api("/api/providers/detect", {});
  toast("Wykrywanie zakończone.");
  route();
}

// ── runs list ────────────────────────────────────────────────────────────────
async function renderRuns() {
  const data = await api("/api/home");
  const all = [...data.sections.attention, ...data.sections.running, ...data.sections.paused, ...data.sections.completed];
  view.innerHTML = `<h1>Zadania</h1><p class="lead">Wszystkie zadania AAW na tym komputerze.</p>
    ${all.length ? `<div class="grid">${all.map(cardHtml).join("")}</div>` : `<div class="section-empty">Brak zadań. <a href="#/new">Utwórz pierwsze</a>.</div>`}`;
  bindCards();
}

// ── new task ─────────────────────────────────────────────────────────────────
function defaultForm() {
  const s = boot.settings;
  return {repo: "", goal: "", first_iteration: "", directions: "", planning: s.planning, implementation: s.implementation,
          review: s.review, base: null,
          advanced: {acceptance_criteria: "", required_evidence: "", forbidden_areas: "", max_iterations: "", max_repair_attempts: "", profile_overrides: {}}};
}
function segHtml(group) {
  const c = boot.choices[group];
  return `<label class="field">${esc(c.label)}</label><div class="seg" data-group="${group}">` +
    c.options.map((o) => `<button type="button" data-value="${esc(o.value)}" class="${formState[group] === o.value ? "on" : ""}">${esc(o.label)}${o.value === c.default ? " ★" : ""}</button>`).join("") +
    `</div><div class="choice-desc" id="desc-${group}"></div>`;
}
function slotOverridesHtml() {
  const profiles = [];
  for (const p of (boot.providers?.providers || [])) for (const r of p.profiles || []) if (r.profile_id) profiles.push(r);
  const opts = (slot) => `<option value="">(wg poziomu)</option>` + profiles.map((p) => `<option value="${esc(p.profile_id)}" ${formState.advanced.profile_overrides[slot] === p.profile_id ? "selected" : ""}>${esc(p.profile_id)}${p.runnable ? "" : " — niedostępny"}</option>`).join("");
  return Object.entries(boot.slot_labels).map(([slot, label]) => `<tr><td>${esc(label)}</td><td><select data-slot="${esc(slot)}">${opts(slot)}</select></td></tr>`).join("");
}
function renderNew() {
  formState = formState || defaultForm();
  const f = formState;
  const based = f.base ? `<div class="note ok">Kontynuacja: start od zaakceptowanego wyniku zadania <span class="mono">${esc(f.base.run_id)}</span> (gałąź <span class="mono">${esc(f.base.branch)}</span>, commit <span class="mono">${esc((f.base.commit || "").slice(0, 10))}</span>). <button class="link" id="drop-base">Zacznij od bazy repozytorium</button></div>` : "";
  view.innerHTML = `<h1>Nowe zadanie</h1><p class="lead">Opisz cel. AAW zaplanuje i wykona iteracje w izolowanej kopii projektu, a na końcu poprosi Cię o decyzję.</p>
  <div class="panel">
    ${based}
    <label class="field">Folder projektu (repozytorium Git)</label>
    <div class="inline"><input type="text" id="repo" placeholder="np. C:\\Projekty\\moja-aplikacja" value="${esc(f.repo)}"><button type="button" id="pick">Wybierz…</button><button type="button" id="check">Sprawdź</button></div>
    <div id="repo-status" class="small" style="margin-top:6px"></div>

    <label class="field">Co chcesz zbudować?</label>
    <textarea id="goal" placeholder="np. Prosta aplikacja do śledzenia wydatków z eksportem do CSV">${esc(f.goal)}</textarea>

    <label class="field">Pierwsza iteracja <span class="hint">(opcjonalnie — konkretny zakres na start)</span></label>
    <textarea id="first" placeholder="np. Model danych wydatku i zapis do pliku JSON z testami">${esc(f.first_iteration)}</textarea>

    <label class="field">Dalszy kierunek <span class="hint">(kilka szerokich punktów, każdy w osobnej linii)</span></label>
    <textarea id="dirs" placeholder="- interfejs w terminalu&#10;- eksport do CSV&#10;- raport miesięczny">${esc(f.directions)}</textarea>
    <div class="note small">Odległe punkty powinny określać kierunek, nie szczegółową implementację. Im dalszy etap, tym większa niepewność co do jego dokładnej formy.</div>

    ${segHtml("planning")}${segHtml("implementation")}${segHtml("review")}

    <details id="adv"><summary>Zaawansowane</summary>
      <label class="field">Kryteria akceptacji pierwszej iteracji <span class="hint">(po jednym w linii; puste = domyślne)</span></label>
      <textarea id="acc">${esc(f.advanced.acceptance_criteria)}</textarea>
      <label class="field">Wymagane dowody <span class="hint">(np. "unit tests"; puste = brak)</span></label>
      <textarea id="ev">${esc(f.advanced.required_evidence)}</textarea>
      <label class="field">Obszary zabronione <span class="hint">(ścieżki, po jednej w linii)</span></label>
      <textarea id="forb">${esc(f.advanced.forbidden_areas)}</textarea>
      <div class="inline" style="margin-top:12px"><label class="small">Maks. iteracji <input type="number" id="maxit" min="1" max="50" value="${esc(f.advanced.max_iterations)}"></label>
      <label class="small">Maks. napraw na iterację <input type="number" id="maxrep" min="1" max="6" value="${esc(f.advanced.max_repair_attempts)}"></label></div>
      <h3 style="margin-top:16px">Dokładne profile ról</h3>
      <table>${slotOverridesHtml()}</table>
    </details>

    <div class="actions"><button class="primary" id="review">Dalej: podsumowanie →</button></div>
  </div>
  <div id="summary"></div>`;
  for (const group of ["planning", "implementation", "review"]) {
    const box = view.querySelector(`[data-group=${group}]`);
    const showDesc = () => {
      const o = boot.choices[group].options.find((x) => x.value === formState[group]);
      document.getElementById(`desc-${group}`).textContent = o ? o.description || "" : "";
    };
    box.querySelectorAll("button").forEach((b) => (b.onclick = () => {
      formState[group] = b.dataset.value;
      box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      showDesc(); clearSummary();
    }));
    showDesc();
  }
  const sync = () => {
    f.repo = val("repo"); f.goal = val("goal"); f.first_iteration = val("first"); f.directions = val("dirs");
    f.advanced.acceptance_criteria = val("acc"); f.advanced.required_evidence = val("ev"); f.advanced.forbidden_areas = val("forb");
    f.advanced.max_iterations = val("maxit"); f.advanced.max_repair_attempts = val("maxrep");
    view.querySelectorAll("[data-slot]").forEach((s) => { if (s.value) f.advanced.profile_overrides[s.dataset.slot] = s.value; else delete f.advanced.profile_overrides[s.dataset.slot]; });
  };
  view.querySelectorAll("input,textarea,select").forEach((el) => el.addEventListener("input", () => { sync(); clearSummary(); }));
  document.getElementById("pick").onclick = async () => {
    const r = await api("/api/pick-folder", {});
    if (r.path) { document.getElementById("repo").value = r.path; sync(); checkRepo(); }
    else if (r.error) toast(r.error);
  };
  document.getElementById("check").onclick = checkRepo;
  document.getElementById("review").onclick = () => { sync(); showSummary(); };
  const drop = document.getElementById("drop-base");
  if (drop) drop.onclick = () => { formState.base = null; renderNew(); };
  if (f.repo && !f.base) checkRepo();
}
const val = (id) => (document.getElementById(id)?.value ?? "").trim();
function clearSummary() { const s = document.getElementById("summary"); if (s) s.innerHTML = ""; }
async function checkRepo() {
  const el = document.getElementById("repo-status");
  const path = val("repo");
  if (!path) { el.innerHTML = ""; return; }
  const r = await api("/api/repo/inspect", {path});
  el.innerHTML = `<div class="note ${r.ready ? "ok" : "warn"}">${esc(r.message)}${r.can_init_git ? ` <button class="link" id="init-git">Utwórz repozytorium Git</button>` : ""}</div>`;
  const b = document.getElementById("init-git");
  if (b) b.onclick = async () => {
    if (!confirm("Utworzyć repozytorium Git w tym folderze i zapisać obecne pliki jako pierwszy commit?")) return;
    try { await api("/api/repo/init", {path}); toast("Repozytorium utworzone."); checkRepo(); } catch (e) { toast(e.message); }
  };
}
function formPayload() {
  const f = formState, a = f.advanced;
  const lines = (t) => t.split("\n").map((x) => x.trim()).filter(Boolean);
  return {repo: f.repo, goal: f.goal, first_iteration: f.first_iteration, directions: lines(f.directions),
          planning: f.planning, implementation: f.implementation, review: f.review, base: f.base,
          advanced: {acceptance_criteria: lines(a.acceptance_criteria), required_evidence: lines(a.required_evidence),
                     forbidden_areas: lines(a.forbidden_areas), max_iterations: a.max_iterations ? Number(a.max_iterations) : null,
                     max_repair_attempts: a.max_repair_attempts ? Number(a.max_repair_attempts) : null,
                     profile_overrides: a.profile_overrides}};
}
function slotRow(s) {
  return `<tr><td>${esc(s.label)}</td><td>${esc(s.display)} <span class="muted small mono">${esc(s.profile_id)}</span>
    <span class="slot-status ${esc(s.status)}">${esc({RECOMMENDED: "rekomendowany", ALTERNATIVE: "alternatywa", OVERRIDE: "ręcznie", UNAVAILABLE: "niedostępny"}[s.status] || s.status)}</span>
    ${s.reason ? `<div class="small muted">${esc(s.reason)}</div>` : ""}</td></tr>`;
}
async function showSummary() {
  const box = document.getElementById("summary");
  box.innerHTML = `<div class="panel muted">Przygotowuję podsumowanie…</div>`;
  let p;
  try { p = await api("/api/tasks/preview", {form: formPayload()}); }
  catch (e) { box.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  const imp = p.implementer_policy, rev = p.review_policy;
  box.innerHTML = `<div class="panel" id="prerun"><h2 style="margin-top:0">Podsumowanie przed startem</h2>
    ${p.blockers.map((b) => `<div class="note bad">${esc(b)}</div>`).join("")}
    ${p.warnings.map((w) => `<div class="note warn">${esc(w)}</div>`).join("")}
    <table class="summary-table">
      <tr><td>Projekt</td><td>${esc(p.project.name || "")} <span class="muted small mono">${esc(p.project.path || "")}</span>${p.project.branch ? ` · ${esc(p.project.branch)} @ ${esc((p.project.head || "").slice(0, 10))}` : ""}</td></tr>
      <tr><td>Cel</td><td>${esc(p.goal)}</td></tr>
      <tr><td>Pierwsza iteracja</td><td>${esc(p.first_iteration)}</td></tr>
      <tr><td>Roadmapa</td><td><ol style="margin:0;padding-left:18px">${p.roadmap.map((r) => `<li>${esc(r.title)}</li>`).join("")}</ol></td></tr>
      <tr><td>Kryteria akceptacji</td><td><ul style="margin:0;padding-left:18px">${p.acceptance_criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></td></tr>
      <tr><td>Limity</td><td>maks. ${esc(p.limits.max_iterations)} iteracji · maks. ${esc(p.limits.max_repair_attempts)} napraw na iterację</td></tr>
      ${slotRow(p.planner)}
      <tr><td colspan="2" class="muted small" style="padding-top:14px">POLITYKA IMPLEMENTACJI — ${esc(p.choices.implementation)}</td></tr>
      ${Object.values(imp).map(slotRow).join("")}
      <tr><td colspan="2" class="muted small" style="padding-top:14px">POLITYKA REVIEW — ${esc(p.choices.review)}</td></tr>
      ${Object.values(rev).map(slotRow).join("")}
      <tr><td>Dostępni providerzy</td><td>${p.providers.map((x) => `${esc(x.display_name)}: ${esc(x.status)}${x.version ? " " + esc(x.version) : ""}${x.status === "FOUND" ? " · " + esc(x.login) : ""}`).join("<br>")}</td></tr>
      <tr><td>Katalog rekomendacji</td><td>${esc(p.catalog.version)} (${esc(p.catalog.source)})</td></tr>
    </table>
    <h3 style="margin-top:16px">Bezpieczeństwo</h3><ul>${p.safety.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
    <div class="actions"><button class="primary" id="start" ${p.can_start ? "" : "disabled"}>START</button></div></div>`;
  box.scrollIntoView({behavior: "smooth"});
  document.getElementById("start").onclick = async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/tasks/start", {form: formPayload()});
      formState = null;
      location.hash = `#/runs/${r.run_id}`;
    } catch (err) { toast(err.message, 8000); e.target.disabled = false; }
  };
}

// ── run detail ───────────────────────────────────────────────────────────────
const MARK = {done: "✓", active: "●", stopped: "❚❚", pending: "○", failed: "✕"};
function processHtml(v) {
  const p = v.process;
  if (!p) {
    const failed = v.status === "START_FAILED";
    return `<div class="panel"><div class="iter-title">START</div><div class="${failed ? "note bad" : "muted"}">${failed ? "Nie udało się wystartować: " + esc(v.status_detail || (v.worker_exit || {}).error || "") : "Uruchamianie procesu AAW…"}</div></div>`;
  }
  const steps = p.nodes.map((n) => `<li class="${n.state}"><span class="mark">${MARK[n.state]}</span>${esc(n.label)}${n.count > 1 ? ` <span class="count">×${n.count}</span>` : ""}</li>`).join("");
  const running = ["RUNNING", "STOPPING"].includes(v.status);
  return `<div class="panel"><div class="process">
    <div><div class="iter-title">ITERACJA ${esc(p.iteration)}</div><ul class="steps">${steps}</ul></div>
    <div class="now">
      <div class="muted small">Aktualnie</div>
      <div class="big">${esc(p.activity || (v.gate ? "Czeka na Twoją decyzję" : v.status_label))}</div>
      <div class="stat-row">
        <div class="stat"><div class="k">Model</div><div class="v">${esc(p.model || "—")}</div></div>
        <div class="stat"><div class="k">Bieżący krok</div><div class="v">${running ? fmtDuration(p.step_elapsed_s) : "—"}</div></div>
        <div class="stat"><div class="k">Czas całkowity</div><div class="v">${fmtDuration(p.run_elapsed_s)}</div></div>
        <div class="stat"><div class="k">Roadmap</div><div class="v">${esc(p.roadmap.done)} / ${esc(p.roadmap.total)}</div></div>
      </div>
      ${v.status_detail ? `<div class="note warn" style="margin-top:12px">${esc(v.status_detail)}</div>` : ""}
      ${stopInfo(v)}
    </div></div>
    <div class="actions">${controlsHtml(v)}</div></div>`;
}
function stopInfo(v) {
  const ev = ((v.stop_effect || {}).events || []).slice(-1)[0];
  if (v.status !== "STOPPING" || !ev) return v.status === "STOPPING" ? `<div class="note warn" style="margin-top:12px">Żądanie zatrzymania wysłane…</div>` : "";
  const text = {PAUSE_AT_NEXT_BOUNDARY: "Bieżący krok zmienia pliki lub jest jednorazowym planem — przerwanie dałoby niepewny stan. AAW dokończy go i zatrzyma się zaraz potem.",
                CANCEL_READ_ONLY_CALL: "Przerwano krok tylko do odczytu; po wznowieniu zostanie powtórzony.",
                FORCE: "Wymuszono natychmiastowe zatrzymanie."}[ev.mode] || ev.mode;
  return `<div class="note warn" style="margin-top:12px">${esc(text)}</div>`;
}
function controlsHtml(v) {
  const c = v.controls, out = [];
  if (c.stop) out.push(`<button class="stop" id="btn-stop">STOP SAFELY</button>`);
  if (c.force_stop) out.push(`<button class="danger" id="btn-force">Zatrzymaj natychmiast…</button>`);
  if (c.resume) out.push(`<button class="primary" id="btn-resume">RESUME</button>`);
  return out.join("");
}
function briefHtml(b) {
  const list = (items) => items.length ? `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<span class="muted">—</span>`;
  const checks = b.checks.length ? `<ul>${b.checks.map((c) => `<li><span class="chk ${esc(c.status)}">${esc(c.status)}</span> ${esc(c.name)}${c.summary ? " — " + esc(c.summary) : ""}</li>`).join("")}</ul>` : `<span class="muted">—</span>`;
  return `<div class="brief ${esc(b.kind)}"><h4><span>${esc(b.phase)}</span><span class="muted small">${esc(fmtTime(b.at))}</span></h4>
    <div class="bgrid">
      <div class="bk">Model</div><div>${esc(b.model)}</div>
      <div class="bk">Cel</div><div>${b.goal ? esc(b.goal) : `<span class="muted">—</span>`}</div>
      <div class="bk">Co zrobiono</div><div>${list(b.done)}</div>
      <div class="bk">Testy / checki</div><div>${checks}</div>
      <div class="bk">Problemy / niepewności</div><div>${list(b.problems)}</div>
      <div class="bk">Przekazano dalej</div><div>${list(b.handed_over)}</div>
    </div>
    ${b.execution_id ? `<div style="margin-top:6px"><button class="link" data-evidence="${esc(b.execution_id)}">Pokaż surowe dowody</button> <span class="muted small mono">${esc(b.execution_id)} · ${esc(b.ledger_state || "")}</span></div>` : ""}
  </div>`;
}
function timelineHtml(v) {
  if (!v.timeline.length) return "";
  return `<h2>Historia</h2>` + v.timeline.map((t) => {
    const cls = t.label.endsWith("PASS") ? "PASS" : /ESKALACJA|PRZERWANA/.test(t.label) ? "bad" : "prog";
    const key = t.iteration_id || "RUN";
    return `<details class="iter" data-iter="${esc(key)}" ${openIterations.has(key) ? "open" : ""}><summary>
      <strong>${t.index ? "Iteracja " + esc(t.index) : "Run"}</strong><span class="label ${cls}">${esc(t.label)}</span>
      <span class="muted small">${esc(t.goal || "")}</span></summary>
      <div class="briefs">${t.briefs.length ? t.briefs.map(briefHtml).join("") : `<div class="muted small">Brak zakończonych etapów.</div>`}</div></details>`;
  }).join("");
}
function gateHtml(v) {
  const g = v.gate;
  if (!g) return "";
  const list = (items, empty) => items.length ? `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="muted small">${empty}</div>`;
  const c = g.candidate, a = g.actions;
  const decided = g.decision ? `<div class="note ${g.decision.decision === "ACCEPTED" ? "ok" : ""}">Decyzja: ${esc(g.decision.decision)} (${esc(fmtTime(g.decision.at))})${(g.decision.next_steps || []).map((s) => `<br>${esc(s)}`).join("")}</div>` : "";
  return `<div class="panel gate"><h2>Human Gate</h2>${decided}
    <div class="sec"><h3>Dlaczego AAW się zatrzymał</h3><div>${esc(g.why)}</div></div>
    <div class="sec"><h3>Co zrobiono</h3>${list(g.done, "Żadna iteracja nie została zaakceptowana.")}</div>
    <div class="sec"><h3>Co zostało</h3>${list(g.remaining, "Nic — roadmapa wyczerpana.")}</div>
    <div class="sec"><h3>Ostrzeżenia</h3>${list(g.warnings, "Brak.")}</div>
    <div class="sec"><h3>Current candidate</h3><div class="kv small">
      <div>Kandydat</div><div class="mono">${esc(c.candidate_id || "—")}</div>
      <div>Gałąź / worktree</div><div class="mono">${esc(c.branch)} · ${esc(c.worktree)}</div>
      <div>HEAD</div><div class="mono">${esc(c.head || "—")}</div>
      <div>Zmienione pliki</div><div class="mono">${esc((c.changed_files || []).join(", ") || "—")}</div>
      <div>Można zaakceptować</div><div>${c.promotable ? "tak" : "nie (eskalacja lub brak zaakceptowanej iteracji)"}</div>
    </div><div class="small muted" style="margin-top:6px">${esc(g.merge_push)}</div></div>
    <div class="actions">
      ${a.accept ? `<button class="primary" id="g-accept">Akceptuj</button>` : ""}
      ${a.add_direction ? `<button id="g-dir">Dodaj dalszy kierunek</button>` : ""}
      ${a.new_goal ? `<button id="g-goal">Kontynuuj z nowym celem</button>` : ""}
      ${a.reject ? `<button class="danger" id="g-reject">Odrzuć</button>` : ""}
      <button id="g-evidence">Pokaż dowody</button>
    </div></div>`;
}
function technicalHtml(v) {
  const w = v.workspace || {}, e = v.engine || {};
  const slots = Object.values((v.resolution || {}).slots || {});
  return `<details><summary>Szczegóły techniczne</summary><div class="panel" style="margin-top:10px"><div class="kv small">
    <div>Run ID</div><div class="mono">${esc(v.run_id)}</div>
    <div>Status silnika</div><div class="mono">${esc(e.status)} / ${esc(e.phase)} · ${esc(e.contract || "")} · ${esc(e.policy_preset || "")}</div>
    <div>Repozytorium</div><div class="mono">${esc(w.repo)}</div>
    <div>Worktree</div><div class="mono">${esc(w.worktree)}</div>
    <div>Gałąź / baza</div><div class="mono">${esc(w.branch)} @ ${esc((w.base_commit || "").slice(0, 12))}</div>
    <div>Bieżące wywołanie</div><div class="mono">${esc((v.process || {}).execution_id || "—")}</div>
    <div>Blokada runu</div><div class="mono">${esc(JSON.stringify((v.controls || {}).lock_token || null))}</div>
  </div><table style="margin-top:12px">${slots.map((s) => `<tr><td>${esc(s.label)}</td><td class="mono">${esc(s.profile_id)} · ${esc(s.status)}</td></tr>`).join("")}</table>
  <div class="actions"><button id="raw-all">Surowe dowody runu</button></div></div></details>`;
}
async function showEvidence(runId, executionId) {
  const q = executionId ? `?execution_id=${encodeURIComponent(executionId)}` : "";
  const data = await api(`/api/runs/${runId}/evidence${q}`);
  modal(executionId ? `Dowody: ${executionId}` : "Surowe dowody runu",
    `<p class="small muted">Dane wprost z artefaktów silnika (ledger, deskryptor, wynik, dziennik). Brief jest tylko ich projekcją.</p><pre class="raw">${esc(JSON.stringify(data, null, 2))}</pre>`);
}
async function renderRun(runId) {
  let last = "";
  const draw = async () => {
    const v = await api(`/api/runs/${runId}`);
    const sig = JSON.stringify(v);
    if (sig === last) return;
    last = sig;
    view.querySelectorAll("details.iter").forEach((d) => d.open ? openIterations.add(d.dataset.iter) : openIterations.delete(d.dataset.iter));
    const techOpen = view.querySelector("details:not(.iter)")?.open;
    view.innerHTML = `<div class="muted small"><a href="#/runs">Zadania</a> › ${esc(v.project || "")}</div>
      <h1>${esc(v.goal)}</h1><p class="lead">${pill(v.status, v.status_label)} <span class="small">utworzono ${esc(fmtTime(v.created_at))}</span></p>
      ${gateHtml(v)}${processHtml(v)}${timelineHtml(v)}${technicalHtml(v)}`;
    if (techOpen) view.querySelector("details:not(.iter)").open = true;
    bindRun(runId, v);
    const active = ["RUNNING", "STOPPING", "STARTING"].includes(v.status);
    setPoll(() => draw().catch(() => {}), active ? 1500 : 5000);
  };
  await draw();
}
function bindRun(runId, v) {
  const on = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
  const act = async (path, body, msg) => {
    try { const r = await api(`/api/runs/${runId}/${path}`, body); if (msg) toast(msg); return r; }
    catch (e) { toast(e.message, 8000); }
  };
  on("btn-stop", () => act("stop", {force: false}, "STOP SAFELY: AAW nie zacznie kolejnego etapu."));
  on("btn-force", () => {
    if (confirm("Zatrzymać natychmiast? Jeśli trwa implementacja lub naprawa, jej skutki w worktree będą NIEPEWNE i po wznowieniu AAW poprosi Cię o decyzję zamiast kontynuować.")) act("stop", {force: true}, "Wymuszono zatrzymanie.");
  });
  on("btn-resume", () => act("resume", {lock_token: v.controls.lock_token}, "Wznawianie…"));
  on("g-accept", async () => {
    const early = v.gate.actions.accept_needs_early_end;
    if (early && !confirm("Roadmapa nie jest wyczerpana. Zaakceptować wynik mimo to (zakończenie wcześniej)?")) return;
    if (!early && !confirm("Zaakceptować ten wynik? AAW nie zrobi merge ani push — zrobisz to sam, gdy zechcesz.")) return;
    await act("accept", {early_end: early}, "Zaakceptowano. Nic nie zostało zmergowane ani wypchnięte.");
  });
  on("g-reject", async () => {
    const reason = prompt("Dlaczego odrzucasz? (opcjonalnie)", "");
    if (reason === null) return;
    await act("reject", {reason}, "Odrzucono.");
  });
  const cont = async (mode) => {
    const promotable = v.gate.candidate.promotable && v.gate.actions.accept;
    const base = promotable ? "Bieżący wynik zostanie zaakceptowany i stanie się bazą nowego zadania (bez merge/push)."
      : (v.gate.actions.reject ? "Tego wyniku nie można zaakceptować — to zadanie zostanie odrzucone jako zastąpione, a nowe wystartuje od bieżącego stanu repozytorium."
         : "Nowe zadanie wystartuje od zaakceptowanego wyniku.");
    const msg = (mode === "direction" ? "Dodać dalszy kierunek? " : "Kontynuować z nowym celem? ") + base;
    if (!confirm(msg)) return;
    const r = await act("continue", {mode});
    if (!r) return;
    formState = Object.assign(defaultForm(), r.prefill);
    formState.directions = (r.prefill.directions || []).join("\n");
    formState.planning = r.prefill.planning || boot.settings.planning;
    formState.implementation = r.prefill.implementation || boot.settings.implementation;
    formState.review = r.prefill.review || boot.settings.review;
    location.hash = "#/new";
  };
  on("g-dir", () => cont("direction"));
  on("g-goal", () => cont("new_goal"));
  on("g-evidence", () => showEvidence(runId));
  on("raw-all", () => showEvidence(runId));
  view.querySelectorAll("[data-evidence]").forEach((b) => (b.onclick = () => showEvidence(runId, b.dataset.evidence)));
}

// ── settings ─────────────────────────────────────────────────────────────────
async function renderSettings() {
  const det = boot.providers;
  const s = boot.settings;
  const providerRows = det ? det.providers.map((p) => `<tr><td><strong>${esc(p.display_name)}</strong></td>
      <td>${p.status === "FOUND" ? `<span class="chk PASS">FOUND</span>` : `<span class="chk FAIL">NOT FOUND</span>`}<div class="small muted mono">${esc(p.executable || "")}</div></td>
      <td>${esc(p.version || "—")}</td>
      <td>${esc({LOGGED_IN: "zalogowano", NOT_LOGGED_IN: "niezalogowano", UNKNOWN: "nieznany"}[p.login] || p.login)}<div class="small muted">${esc(p.login_detail || "")}</div></td>
      <td class="small">${(p.profiles || []).filter((r) => r.profile_id).map((r) => `<span class="mono">${esc(r.profile_id)}</span> ${r.runnable ? "✓" : `<span class="muted">✕</span>`}`).join("<br>")}</td></tr>
      ${p.status !== "FOUND" || p.login === "NOT_LOGGED_IN" ? `<tr><td></td><td colspan="4" class="small"><div class="note">${esc(p.setup_help.install)}<br>${esc(p.setup_help.login)}<br>Sprawdzenie: <span class="mono">${esc(p.setup_help.verify)}</span></div></td></tr>` : ""}`).join("") : "";
  const sel = (group) => `<select data-setting="${group}">${boot.choices[group].options.map((o) => `<option value="${esc(o.value)}" ${s[group] === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
  view.innerHTML = `<h1>Ustawienia</h1><p class="lead">${esc(boot.notice)}</p>
    <div class="panel"><h3>Providerzy (CLI AI)</h3>
      ${det ? `<div class="small muted">Wykryto: ${esc(fmtTime(det.detected_at))} · Git: ${esc(det.git.status)} ${esc(det.git.version || "")}</div>` : `<div class="note warn">Jeszcze nie wykryto.</div>`}
      <table style="margin-top:8px"><tr><th>CLI</th><th>Status</th><th>Wersja</th><th>Logowanie</th><th>Profile AAW</th></tr>${providerRows}</table>
      <div class="actions"><button class="primary" id="redetect">Wykryj ponownie</button></div></div>
    <div class="panel"><h3>Domyślne poziomy</h3><div class="kv">
      <div>Siła planowania</div><div>${sel("planning")}</div>
      <div>Implementacja</div><div>${sel("implementation")}</div>
      <div>Review</div><div>${sel("review")}</div>
      <div>Aktualizacje rekomendacji online</div><div><label><input type="checkbox" id="online" ${s.online_recommendation_updates ? "checked" : ""}> sprawdzaj przy starcie (tylko odczyt pliku z GitHub AAW)</label></div>
      <div>Katalog rekomendacji</div><div>${esc(boot.catalog.version)} (${esc(boot.catalog.source)}) <button class="link" id="upd">Sprawdź aktualizację teraz</button></div>
    </div><div class="actions"><button class="primary" id="save">Zapisz</button></div></div>
    <details><summary>Zaawansowane</summary><div class="panel" style="margin-top:10px">
      <div class="kv small"><div>Folder danych</div><div class="mono">${esc(boot.data_home)}</div>
      <div>Wersja</div><div>${esc(boot.version)}${boot.frozen ? " (portable)" : " (źródła)"}</div></div>
      <div id="recs"></div>
      <div class="actions"><button id="show-recs">Pokaż katalog rekomendacji</button>
      ${boot.control_center_available ? `<button id="cc">Otwórz konsolę operatora (Control Center)</button>` : ""}</div>
    </div></details>
    <div class="panel" style="margin-top:16px"><h3>Zamknij AAW</h3><p class="small muted">Zamyka okno aplikacji. Trwające zadania pracują dalej w tle; po ponownym uruchomieniu AAW zobaczysz ich stan.</p>
      <button class="danger" id="quit">Zamknij AAW</button></div>`;
  document.getElementById("redetect").onclick = async () => { await detectNow(); };
  document.getElementById("save").onclick = async () => {
    const body = {online_recommendation_updates: document.getElementById("online").checked};
    view.querySelectorAll("[data-setting]").forEach((el) => (body[el.dataset.setting] = el.value));
    try { boot.settings = await api("/api/settings", body); toast("Zapisano."); } catch (e) { toast(e.message); }
  };
  document.getElementById("upd").onclick = async () => {
    const r = await api("/api/recommendations/update", {});
    toast(`${r.status}: ${r.detail}`, 7000);
    boot = await api("/api/bootstrap");
  };
  document.getElementById("show-recs").onclick = async () => {
    const r = await api("/api/recommendations");
    document.getElementById("recs").innerHTML = `<table style="margin-top:10px"><tr><th>Profil</th><th>Provider</th><th>Role</th><th>Siła/szybkość/koszt</th><th>Status</th><th>Tutaj</th></tr>${r.profiles.map((p) => `<tr><td class="mono">${esc(p.profile)}</td><td>${esc(p.provider)}</td><td class="small">${esc(p.recommended_roles.join(", "))}</td><td class="small">${esc(p.strength_class)} / ${esc(p.speed_class)} / ${esc(p.cost_class)}</td><td class="small">${esc(p.status)}</td><td>${p.runnable_here ? "✓" : "✕"}</td></tr>`).join("")}</table>`;
  };
  const cc = document.getElementById("cc");
  if (cc) cc.onclick = async () => { try { await api("/api/advanced/control-center", {}); toast("Uruchomiono Control Center."); } catch (e) { toast(e.message); } };
  document.getElementById("quit").onclick = async () => {
    if (!confirm("Zamknąć AAW? Trwające zadania będą kontynuowane w tle.")) return;
    const r = await api("/api/quit", {});
    view.innerHTML = `<div class="panel"><h2>AAW zamknięty</h2><p>${esc(r.note)}</p><p class="muted">Możesz zamknąć tę kartę. Aby wrócić, uruchom ponownie AAW.exe.</p></div>`;
    setPoll(null);
  };
}

// ── boot ─────────────────────────────────────────────────────────────────────
(async function start() {
  try {
    boot = await api("/api/bootstrap");
  } catch (e) {
    view.innerHTML = `<div class="note bad">Nie można połączyć się z AAW: ${esc(e.message)}</div>`;
    return;
  }
  document.getElementById("nav-foot").textContent = boot.version;
  if (!boot.providers) {
    try { boot.providers = await api("/api/providers/detect", {}); } catch (e) { /* shown on Home */ }
  }
  if (boot.settings.online_recommendation_updates) {
    api("/api/recommendations/update", {}).then(async (r) => { if (r.status === "UPDATED") { toast("Zaktualizowano katalog rekomendacji (dotyczy nowych zadań)."); boot = await api("/api/bootstrap"); } }).catch(() => {});
  }
  route();
})();
