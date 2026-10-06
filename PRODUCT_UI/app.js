"use strict";
// AAW Product MVP V0.2 — single-page UI. Every number and status shown here
// comes from the local API, which projects the engine's own artifacts.

const TOKEN = document.querySelector('meta[name="aaw-token"]').content;
const view = document.getElementById("view");
let boot = null;          // /api/bootstrap
let pollTimer = null;
let formState = null;     // New Task wizard (kept while navigating)
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
const val = (id) => (document.getElementById(id)?.value ?? "").trim();
const on = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };

// ── router ───────────────────────────────────────────────────────────────────
async function route() {
  const hash = location.hash || "#/home";
  const [, page, arg] = hash.split("/");
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === (page === "runs" && arg ? "runs" : page)));
  setPoll(null);
  document.body.classList.remove("show-adv");
  try {
    if (page === "new") return renderWizard();
    if (page === "runs" && arg) return renderRun(arg);
    if (page === "runs") return renderRuns();
    if (page === "settings") return renderSettings();
    return renderHome();
  } catch (e) {
    view.innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// ── providers (shared by the wizard and Settings) ───────────────────────────
const LOGIN_TEXT = {LOGGED_IN: "zalogowano", NOT_LOGGED_IN: "NIE zalogowano", UNKNOWN: "nie udało się sprawdzić"};
const STATE_CLASS = {AVAILABLE: "PASS", VERIFIED_HERE: "PASS", NOT_VERIFIED: "WARN", NEEDS_CHECK: "WARN",
                     REJECTED_HERE: "FAIL", POLICY_UNAVAILABLE: "SKIPPED", CLI_NOT_FOUND: "SKIPPED", NOT_LOGGED_IN: "FAIL"};
function providerReady(p) { return p.status === "FOUND" && p.login !== "NOT_LOGGED_IN"; }
function providerCard(p) {
  const found = p.status === "FOUND";
  const ready = providerReady(p);
  const head = found ? (ready ? `<span class="chk PASS">FOUND</span>` : `<span class="chk FAIL">FOUND — niezalogowane</span>`) : `<span class="chk FAIL">NOT FOUND</span>`;
  const models = (p.models || []).filter((m) => m.state !== "CLI_NOT_FOUND");
  const help = (!found || p.login === "NOT_LOGGED_IN") ? `<div class="note small">
      ${!found ? `<div><strong>1. Instalacja:</strong> ${esc(p.setup_help.install)}</div>` : ""}
      <div><strong>${found ? "" : "2. "}Logowanie:</strong> ${esc(p.setup_help.login)}</div>
      <div><strong>Sprawdzenie (opcjonalnie):</strong> <span class="mono">${esc(p.setup_help.verify)}</span></div></div>` : "";
  const loginWarn = found && p.login === "UNKNOWN" ? `<div class="note warn small">Nie udało się bezpiecznie sprawdzić logowania (${esc(p.login_detail || "")}). Jeśli CLI nie jest zalogowane, AAW zatrzyma się na pierwszym kroku.</div>` : "";
  return `<div class="pcard ${ready ? "ready" : ""}">
    <div class="pcard-head"><strong>${esc(p.display_name)}</strong>${head}</div>
    <div class="kv small">
      <div>Wersja</div><div>${esc(p.version || "—")}</div>
      <div>Logowanie</div><div>${found ? esc(LOGIN_TEXT[p.login] || p.login) : "—"}</div>
      ${found ? `<div>Modele</div><div>${models.length ? models.map((m) => `<div><span class="chk ${STATE_CLASS[m.state] || ""}">●</span> ${esc(m.model_family || m.runtime_model_id)} <span class="muted">— ${esc(m.label)}</span></div>`).join("") : "—"}</div>` : ""}
    </div>${loginWarn}${help}</div>`;
}
function providersHtml(det) {
  if (!det) return `<div class="note warn">Jeszcze nie sprawdzono dostępnych CLI AI.</div>`;
  const git = det.git || {};
  const gitNote = git.status === "FOUND" ? `<span class="chk PASS">Git ${esc(git.version || "")}</span>` :
    `<div class="note bad">Git nie jest zainstalowany. ${esc(git.help || "")}</div>`;
  return `<div class="pgrid">${det.providers.map(providerCard).join("")}</div>
    <div class="small muted" style="margin-top:8px">Sprawdzono: ${esc(fmtTime(det.detected_at))} · ${gitNote}</div>`;
}
async function detectNow() {
  toast("Wykrywanie CLI…");
  boot.providers = await api("/api/providers/detect", {});
  toast("Wykrywanie zakończone.");
}

// ── home ─────────────────────────────────────────────────────────────────────
function cardHtml(c) {
  const road = c.roadmap ? `Roadmapa ${c.roadmap.done} / ${c.roadmap.total}` : "";
  return `<div class="card" data-run="${esc(c.run_id)}">
    <div class="row"><span class="proj">${esc(c.project || "")}</span>${pill(c.status, c.status_label)}</div>
    <div class="goal">${esc(c.short_goal || c.goal)}</div>
    <div class="row"><span>${c.iteration ? "Iteracja " + esc(c.iteration) : ""}${c.phase ? " · <strong>" + esc(c.phase) + "</strong>" : ""}</span><span>${esc(road)}</span></div>
    <div class="row"><span>${esc(c.activity || "")}</span><span title="ostatnia aktywność">${esc(fmtTime(c.last_activity))}</span></div>
    ${c.can_resume ? `<div class="card-actions"><button class="primary" data-resume="${esc(c.run_id)}" data-token="${esc(c.lock_token || "")}">RESUME</button><span class="small muted">wznawia bezpiecznie od zapisanego miejsca</span></div>` : ""}
  </div>`;
}
function bindCards() {
  view.querySelectorAll("[data-run]").forEach((el) => (el.onclick = (e) => {
    if (e.target.closest("[data-resume]")) return;
    location.hash = `#/runs/${el.dataset.run}`;
  }));
  view.querySelectorAll("[data-resume]").forEach((b) => (b.onclick = async (e) => {
    e.stopPropagation(); b.disabled = true;
    try { await api(`/api/runs/${b.dataset.resume}/resume`, {lock_token: b.dataset.token || null}); toast("Wznawianie…"); location.hash = `#/runs/${b.dataset.resume}`; }
    catch (err) { toast(err.message, 8000); b.disabled = false; }
  }));
}
function providerBanner() {
  const det = boot.providers;
  if (!det) return `<div class="note warn">Nie sprawdzono jeszcze dostępnych CLI AI. <a href="#/settings">Ustawienia</a></div>`;
  if (!det.providers.some(providerReady)) return `<div class="note bad">Brak gotowego CLI AI (Claude CLI lub Codex CLI, zainstalowane i zalogowane). <a href="#/new">Pokaż, jak to przygotować</a>.</div>`;
  const rows = det.providers.filter((p) => p.status === "FOUND").map((p) => `${esc(p.display_name)} ${esc(p.version || "")} — ${esc(LOGIN_TEXT[p.login] || p.login)}`);
  return `<div class="note small">Gotowe CLI: ${rows.join(" · ")} · <a href="#/settings">Ustawienia</a></div>`;
}
async function renderHome() {
  const draw = async () => {
    const data = await api("/api/home");
    const s = data.sections;
    const sec = (key, title, list, empty) => `<section class="home-sec ${key}"><h2>${title} <span class="count-badge">${list.length}</span></h2>` +
      (list.length ? `<div class="grid">${list.map(cardHtml).join("")}</div>` : `<div class="section-empty">${empty}</div>`) + `</section>`;
    const total = s.running.length + s.paused.length + s.attention.length + s.completed.length;
    view.innerHTML = `<div class="page-head"><div><h1>Home</h1><p class="lead">Co pracuje, co jest wstrzymane i co czeka na Ciebie.</p></div>
      <button class="primary big-btn" onclick="location.hash='#/new'">+ Nowe zadanie</button></div>
      ${providerBanner()}
      ${total ? "" : `<div class="panel empty-home"><h2 style="margin-top:0">Witaj w AAW</h2><p>Nie masz jeszcze żadnych zadań. Kliknij <strong>+ Nowe zadanie</strong> — kreator przeprowadzi Cię przez 7 krótkich kroków do przycisku START.</p></div>`}
      ${sec("running", "W toku", s.running, "Nic teraz nie pracuje.")}
      ${sec("paused", "Wstrzymane", s.paused, "Brak wstrzymanych ani przerwanych zadań.")}
      ${sec("attention", "Wymaga uwagi", s.attention, "Nic nie czeka na Twoją decyzję.")}
      ${sec("completed", "Zakończone", s.completed, "Brak zakończonych zadań.")}`;
    bindCards();
  };
  await draw();
  setPoll(() => draw().catch(() => {}), 3000);
}

// ── runs list ────────────────────────────────────────────────────────────────
async function renderRuns() {
  const data = await api("/api/home");
  const all = [...data.sections.running, ...data.sections.paused, ...data.sections.attention, ...data.sections.completed];
  view.innerHTML = `<h1>Zadania</h1><p class="lead">Wszystkie zadania AAW na tym komputerze.</p>
    ${all.length ? `<div class="grid">${all.map(cardHtml).join("")}</div>` : `<div class="section-empty">Brak zadań. <a href="#/new">Utwórz pierwsze</a>.</div>`}`;
  bindCards();
}

// ── new task wizard ──────────────────────────────────────────────────────────
const STEPS = ["Projekt", "Narzędzia AI", "Modele", "Cel", "Pierwsza iteracja", "Kierunek", "Start"];
const EXAMPLES = [
  {title: "Tracker wydatków (Python)", goal: "Prosta aplikacja w Pythonie do śledzenia wydatków: dodawanie wydatków, lista i eksport do CSV.",
   first: "Model danych wydatku (kwota, kategoria, data, opis) i zapis/odczyt z pliku JSON, z testami jednostkowymi.",
   dirs: "- polecenia w terminalu: dodaj, lista, usuń\n- eksport do CSV\n- podsumowanie miesięczne"},
  {title: "Testy i walidacja w istniejącym projekcie", goal: "Zwiększ niezawodność istniejącego kodu: testy dla obecnego zachowania i czytelne błędy przy złych danych wejściowych.",
   first: "Dodaj testy jednostkowe opisujące obecne zachowanie głównego modułu (bez zmiany logiki).",
   dirs: "- walidacja danych wejściowych z czytelnymi komunikatami\n- krótka dokumentacja w README"},
  {title: "Lista zadań w przeglądarce (HTML/JS)", goal: "Mała strona HTML/JavaScript z listą zadań do zrobienia, działająca bez serwera.",
   first: "Strona index.html z dodawaniem i usuwaniem zadań zapisywanych w localStorage.",
   dirs: "- filtrowanie: wszystkie / zrobione / do zrobienia\n- prosty, czytelny wygląd\n- eksport listy do pliku JSON"},
];
function defaultForm() {
  const s = boot.settings;
  return {step: boot.settings.first_run_completed ? 0 : -1, repo: "", repoInfo: null, goal: "", first_iteration: "", directions: "",
          planning: s.planning, implementation: s.implementation, review: s.review, implementer_chain: null, base: null, setup: null, preview: null,
          advanced: {acceptance_criteria: "", required_evidence: "", forbidden_areas: "", max_iterations: "", max_repair_attempts: "", profile_overrides: {}}};
}
function stepperHtml(step) {
  return `<ol class="stepper">${STEPS.map((name, i) => `<li class="${i < step ? "done" : i === step ? "now" : ""}" data-goto="${i}"><span>${i < step ? "✓" : i + 1}</span>${esc(name)}</li>`).join("")}</ol>`;
}
function stepOk(step) {
  const f = formState;
  if (step === 0) return !!f.base || !!(f.repoInfo && f.repoInfo.ready);
  if (step === 1) return !!(boot.providers && boot.providers.providers.some(providerReady)) && (boot.providers.git || {}).status === "FOUND";
  if (step === 2) return !!(f.setup && !f.setup.blockers.length);
  if (step === 3) return f.goal.trim().length >= 5;
  return true;
}
async function renderWizard() {
  formState = formState || defaultForm();
  const f = formState;
  if (f.step === -1) return renderWelcome();
  const body = [stepProject, stepProviders, stepModels, stepGoal, stepFirst, stepDirections, stepStart][f.step];
  view.innerHTML = `<h1>Nowe zadanie</h1>${stepperHtml(f.step)}<div class="panel wizard" id="wiz"></div>
    <div class="wiz-nav">${f.step > 0 ? `<button id="back">← Wstecz</button>` : `<span></span>`}
    ${f.step < STEPS.length - 1 ? `<button class="primary" id="next" ${stepOk(f.step) ? "" : "disabled"}>Dalej →</button>` : ""}</div>`;
  view.querySelectorAll("[data-goto]").forEach((li) => (li.onclick = () => {
    const target = Number(li.dataset.goto);
    for (let i = 0; i < target; i++) if (!stepOk(i)) { toast(`Najpierw uzupełnij krok ${i + 1}: ${STEPS[i]}.`); return; }
    f.step = target; renderWizard();
  }));
  on("back", () => { f.step -= 1; renderWizard(); });
  on("next", () => { if (stepOk(f.step)) { f.step += 1; renderWizard(); } });
  await body(document.getElementById("wiz"));
}
function refreshNext() { const n = document.getElementById("next"); if (n) n.disabled = !stepOk(formState.step); }

function renderWelcome() {
  view.innerHTML = `<div class="panel welcome"><h1>Witaj w AAW</h1>
    <p class="lead">AAW samodzielnie planuje i wykonuje kolejne iteracje pracy nad Twoim projektem, a na końcu prosi Cię o decyzję.</p>
    <ul class="facts">
      <li><strong>Potrzebujesz co najmniej jednego CLI AI</strong> — Claude CLI lub Codex CLI — zainstalowanego i zalogowanego na Twoim koncie. Instalacja i logowanie są poza AAW; AAW tylko je wykrywa.</li>
      <li><strong>AAW pracuje w izolowanej kopii projektu.</strong> Wybierasz swój zwykły folder; AAW sam tworzy bezpieczną kopię roboczą i nie zmienia Twoich plików.</li>
      <li><strong>Brak automatycznego merge i push.</strong> Na końcu dostajesz Human Gate: akceptujesz, odrzucasz albo wskazujesz dalszy kierunek.</li>
      <li><strong>Możesz bezpiecznie zatrzymać i wznowić</strong> pracę w dowolnym momencie.</li>
    </ul>
    <p class="small muted">Kreator ma 7 kroków: projekt → narzędzia AI → modele → cel → pierwsza iteracja → kierunek → START.</p>
    <div class="actions"><button class="primary big-btn" id="go">Zaczynamy →</button></div></div>`;
  on("go", async () => { formState.step = 0; try { boot.settings = await api("/api/first-run/done", {}); } catch (e) { /* not critical */ } renderWizard(); });
}

async function stepProject(box) {
  const f = formState;
  if (f.base) {
    box.innerHTML = `<h2>1. Projekt</h2><div class="note ok">Kontynuacja: nowe zadanie startuje od zaakceptowanego wyniku zadania
      <span class="mono">${esc(f.base.run_id)}</span>. Nic nie zostało zmergowane ani wypchnięte.</div>
      <button class="link" id="drop-base">Zamiast tego zacznij od bieżącego stanu repozytorium</button>`;
    on("drop-base", () => { f.base = null; renderWizard(); });
    return;
  }
  box.innerHTML = `<h2>1. Wybierz folder projektu</h2>
    <p class="hint">Twój zwykły folder z projektem (repozytorium Git). AAW sam przygotuje izolowaną kopię roboczą — nie musisz niczego konfigurować.</p>
    <div class="inline"><input type="text" id="repo" placeholder="np. C:\\Projekty\\moja-aplikacja" value="${esc(f.repo)}"><button type="button" id="pick">Wybierz…</button><button type="button" id="check">Sprawdź</button></div>
    ${(boot.recent_repos || []).length ? `<div class="examples"><span class="small muted">Ostatnio używane:</span>${boot.recent_repos.map((r) => `<button class="link" data-recent="${esc(r)}">${esc(r)}</button>`).join("")}</div>` : ""}
    <div id="repo-status" style="margin-top:10px"></div>`;
  const input = document.getElementById("repo");
  input.addEventListener("input", () => { f.repo = input.value.trim(); f.repoInfo = null; refreshNext(); });
  input.addEventListener("change", checkRepo);
  on("pick", async () => {
    const r = await api("/api/pick-folder", {});
    if (r.path) { input.value = r.path; f.repo = r.path; checkRepo(); }
    else if (r.error) toast(r.error, 7000);
  });
  on("check", checkRepo);
  box.querySelectorAll("[data-recent]").forEach((b) => (b.onclick = () => { input.value = b.dataset.recent; f.repo = b.dataset.recent; checkRepo(); }));
  if (f.repo) checkRepo();
}
async function checkRepo() {
  const f = formState;
  const el = document.getElementById("repo-status");
  f.repo = val("repo");
  if (!f.repo) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="muted small">Sprawdzam…</div>`;
  const r = await api("/api/repo/inspect", {path: f.repo});
  f.repoInfo = r;
  const files = (r.dirty_files || []).length ? `<details><summary>Pliki (${esc(r.dirty_count)})</summary><div class="mono small">${r.dirty_files.map(esc).join("<br>")}${r.dirty_count > r.dirty_files.length ? "<br>…" : ""}</div></details>` : "";
  el.innerHTML = `<div class="note ${r.ready ? "ok" : "warn"}">${esc(r.message)}${r.can_init_git ? `<div style="margin-top:8px"><button id="init-git">Utwórz repozytorium Git</button></div>` : ""}</div>
    ${r.subfolder_note ? `<div class="note small">${esc(r.subfolder_note)}</div>` : ""}${files}
    ${r.ready ? `<div class="small muted">Projekt: <strong>${esc(r.name)}</strong></div>` : ""}`;
  on("init-git", async () => {
    if (!confirm("Utworzyć repozytorium Git w tym folderze i zapisać obecne pliki jako pierwszy commit? Nic nie zostanie usunięte.")) return;
    try { await api("/api/repo/init", {path: f.repo}); toast("Repozytorium utworzone."); checkRepo(); } catch (e) { toast(e.message, 8000); }
  });
  refreshNext();
}

async function stepProviders(box) {
  const draw = () => {
    const det = boot.providers;
    const ready = det && det.providers.some(providerReady);
    box.innerHTML = `<h2>2. Narzędzia AI na tym komputerze</h2>
      <p class="hint">${esc(boot.setup_notice)}</p>
      ${providersHtml(det)}
      ${ready ? `<div class="note ok">Gotowe — AAW może pracować z: ${det.providers.filter(providerReady).map((p) => esc(p.display_name)).join(", ")}.</div>` :
        `<div class="note bad">Nie ma jeszcze gotowego CLI. Zainstaluj i zaloguj co najmniej jedno (instrukcje powyżej), potem kliknij „Wykryj ponownie”. AAW nie musi być zamykane.</div>`}
      <div class="actions"><button id="redetect">Wykryj ponownie</button></div>`;
    on("redetect", async (e) => { e.target.disabled = true; try { await detectNow(); } finally { draw(); refreshNext(); } });
    refreshNext();
  };
  if (!boot.providers) { box.innerHTML = `<div class="muted">Wykrywanie CLI…</div>`; try { await detectNow(); } catch (e) { toast(e.message); } }
  draw();
}

function slotLine(s) {
  const tag = {RECOMMENDED: "rekomendowany", ALTERNATIVE: "alternatywa", OVERRIDE: "ręcznie", UNAVAILABLE: "niedostępny"}[s.status] || s.status;
  return `<div class="slotline"><span class="slot-name">${esc(s.label)}</span> <strong>${esc(s.display)}</strong>
    <span class="slot-status ${esc(s.status)}">${esc(tag)}</span>${s.availability === "NOT_VERIFIED" ? ` <span class="slot-status ALTERNATIVE">niesprawdzony</span>` : ""}
    ${s.reason ? `<div class="small muted">${esc(s.reason)}</div>` : ""}</div>`;
}
function groupHtml(group, setup) {
  const c = boot.choices[group];
  const g = setup ? setup.groups[group] : null;
  const f = formState;
  const desc = (c.options.find((o) => o.value === f[group]) || {}).description || "";
  const names = {planning: "PLANOWANIE", implementation: "IMPLEMENTACJA", review: "REVIEW"};
  return `<div class="group"><div class="group-head">${names[group]}</div>
    <div class="seg" data-group="${group}">${c.options.map((o) => `<button type="button" data-value="${esc(o.value)}" class="${f[group] === o.value ? "on" : ""}">${esc(o.label)}${o.value === c.default ? " ★" : ""}</button>`).join("")}</div>
    <div class="choice-desc">${esc(desc)}</div>
    ${group === "implementation" ? chainHtml(setup) : ""}
    <div class="models-under">${g ? (group === "implementation" && setup.implementer_chain && setup.implementer_chain.source !== "SLOTS" ? "" : g.main.map(slotLine).join("")) : `<span class="muted small">…</span>`}
      ${g && g.all.length > g.main.length ? `<details><summary class="small">Pozostałe etapy (${g.all.length - g.main.length})</summary>${g.all.filter((s) => !g.main.includes(s) && !g.main.some((m) => m.slot === s.slot)).map(slotLine).join("")}</details>` : ""}
    </div></div>`;
}
const CHAIN_SLOTS = ["implementer_default", "implementer_harder", "implementer_hard", "implementer_capability_escalation", "repair_default", "repair_hard", "review_pretreatment"];
function setupPayload() {
  const f = formState;
  return {choices: {planning: f.planning, implementation: f.implementation, review: f.review}, implementer_chain: f.implementer_chain};
}
async function loadSetup() {
  const f = formState;
  f.setup = await api("/api/setup/resolve", setupPayload());
  return f.setup;
}
// Selectable profiles for the implementer chain: every profile with an exact runtime model.
function chainProfiles() {
  const out = [];
  for (const p of (boot.providers?.providers || [])) for (const r of p.profiles || []) if (r.profile_id && r.runtime_model_id) out.push(r);
  return out;
}
// The chain being edited: the user's own, or the effective one resolved by the system (default).
function effectiveChain() {
  const f = formState;
  if (f.implementer_chain) return f.implementer_chain.slice();
  const c = f.setup && f.setup.implementer_chain;
  return c && c.steps && c.steps.length ? c.steps.map((x) => x.profile_id) : [];
}
function chainHtml(setup) {
  const f = formState;
  const c = setup && setup.implementer_chain;
  const source = f.implementer_chain ? "USER" : (c ? c.source : "SLOTS");
  const rows = effectiveChain();
  const steps = (c && c.steps) || [];
  const profiles = chainProfiles();
  const tag = {USER: "ręczny", DEFAULT: "domyślny AAW", SLOTS: "wg poziomu"}[source] || source;
  const opt = (id, current) => {
    const p = profiles.find((x) => x.profile_id === id);
    const taken = rows.includes(id) && id !== current;
    return `<option value="${esc(id)}" ${id === current ? "selected" : ""} ${taken ? "disabled" : ""}>${esc(p ? (p.display_name || id) : id)} — ${esc(p ? p.runtime_model_id : "brak ID")} / ${esc(p ? p.effort : "")}${p && !p.runnable ? " (niedostępny)" : ""}</option>`;
  };
  const list = rows.map((id, i) => {
    const st = steps.find((x) => x.profile_id === id);
    const avail = st && !st.runnable ? ` <span class="slot-status UNAVAILABLE">niedostępny</span>` : (st && st.availability === "NOT_VERIFIED" ? ` <span class="slot-status ALTERNATIVE">niesprawdzony</span>` : "");
    return `<li class="chain-step"><span class="chain-n">${i + 1}</span>
      <select data-chain-step="${i}">${profiles.map((p) => opt(p.profile_id, id)).join("")}${profiles.some((p) => p.profile_id === id) ? "" : opt(id, id)}</select>${avail}
      <span class="chain-btns"><button type="button" data-chain-up="${i}" ${i === 0 ? "disabled" : ""} title="Wyżej">↑</button><button type="button" data-chain-down="${i}" ${i === rows.length - 1 ? "disabled" : ""} title="Niżej">↓</button><button type="button" data-chain-del="${i}" ${rows.length <= 1 ? "disabled" : ""} title="Usuń krok">✕</button></span></li>`;
  }).join("");
  const skipped = (c && c.skipped || []).map((x) => `<div class="small muted">pominięty (niedostępny tu): ${esc(x.display)}${x.reason ? " — " + esc(x.reason) : ""}</div>`).join("");
  return `<div class="chain"><div class="chain-head">Łańcuch implementatora <span class="slot-status ${source === "DEFAULT" ? "RECOMMENDED" : "OVERRIDE"}">${esc(tag)}</span></div>
    ${rows.length ? `<ol class="chain-list">${list}</ol>` : `<div class="small muted">Etapy implementacji wynikają z wybranego poziomu (poniżej). Kliknij „Własny łańcuch”, aby samemu wybrać model(e) implementatora.</div>`}
    ${skipped}
    <div class="chain-actions">
      ${rows.length ? `<button type="button" id="chain-add" ${rows.length >= 12 ? "disabled" : ""}>+ Dodaj krok eskalacji</button>` : `<button type="button" id="chain-custom">Własny łańcuch</button>`}
      ${f.implementer_chain ? `<button type="button" id="chain-default" class="link">Przywróć domyślny</button>` : ""}
    </div>
    <p class="small muted">Krok 1 to implementator na starcie; kolejne kroki to eskalacja, gdy poprzedni model nie wystarcza (rosnąca trudność zadania, błąd możliwości, naprawy). Jeden krok = jeden model na całą implementację. Krok niedostępny nigdy nie jest podmieniany po cichu.</p></div>`;
}
async function stepModels(box) {
  const f = formState;
  box.innerHTML = `<h2>3. Modele</h2><div class="muted">Dobieram modele…</div>`;
  const draw = () => {
    const s = f.setup;
    const checkable = s ? [...new Set(Object.values(s.groups).flatMap((g) => g.checkable))] : [];
    box.innerHTML = `<h2>3. Potwierdź zestaw modeli</h2>
      <p class="hint">Wybierz trzy proste poziomy. Pod spodem widać, jakie modele faktycznie zostaną użyte — AAW nigdy nie podmienia modelu po cichu. ★ = rekomendacja.</p>
      ${groupHtml("planning", s)}${groupHtml("implementation", s)}${groupHtml("review", s)}
      ${(s ? s.blockers : []).map((b) => `<div class="note bad">${esc(b)}</div>`).join("")}
      ${(s ? s.warnings : []).map((w) => `<div class="note warn small">${esc(w)}</div>`).join("")}
      ${checkable.length ? `<div class="panel-sub"><strong>Sprawdź modele na tym komputerze</strong>
        <p class="small muted">Niektóre modele (${checkable.length}) trzeba jeszcze potwierdzić. AAW wyśle po jednym bardzo krótkim zapytaniu do każdego z nich przez Twoje CLI (zużywa minimalną część limitu). Wynik zostaje zapamiętany.</p>
        <button id="verify">Sprawdź modele</button> <span id="verify-status" class="small muted"></span></div>` : ""}
      <details class="small" style="margin-top:14px"><summary>Zaawansowane: dokładne profile ról</summary>${advancedSlotsHtml()}</details>`;
    for (const group of ["planning", "implementation", "review"]) {
      box.querySelectorAll(`[data-group=${group}] button`).forEach((b) => (b.onclick = async () => {
        f[group] = b.dataset.value; await loadSetup(); draw();
      }));
    }
    const setChain = async (ids) => {
      f.implementer_chain = ids && ids.length ? ids : null;
      if (f.implementer_chain) CHAIN_SLOTS.forEach((k) => delete f.advanced.profile_overrides[k]);
      try { await loadSetup(); } catch (e) { toast(e.message, 8000); }
      draw();
    };
    box.querySelectorAll("[data-chain-step]").forEach((sel) => (sel.onchange = () => {
      const ids = effectiveChain(); ids[Number(sel.dataset.chainStep)] = sel.value; setChain(ids);
    }));
    box.querySelectorAll("[data-chain-up]").forEach((b) => (b.onclick = () => {
      const ids = effectiveChain(), i = Number(b.dataset.chainUp); [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]]; setChain(ids);
    }));
    box.querySelectorAll("[data-chain-down]").forEach((b) => (b.onclick = () => {
      const ids = effectiveChain(), i = Number(b.dataset.chainDown); [ids[i + 1], ids[i]] = [ids[i], ids[i + 1]]; setChain(ids);
    }));
    box.querySelectorAll("[data-chain-del]").forEach((b) => (b.onclick = () => {
      const ids = effectiveChain(); ids.splice(Number(b.dataset.chainDel), 1); setChain(ids);
    }));
    on("chain-add", () => {
      const ids = effectiveChain();
      const next = chainProfiles().find((p) => !ids.includes(p.profile_id) && p.runnable) || chainProfiles().find((p) => !ids.includes(p.profile_id));
      if (next) setChain([...ids, next.profile_id]);
    });
    on("chain-custom", () => {
      const first = s && s.groups.implementation.main[0];
      if (first) setChain([first.profile_id]);
    });
    on("chain-default", () => setChain(null));
    box.querySelectorAll("[data-slot]").forEach((sel) => (sel.onchange = () => {
      if (sel.value) f.advanced.profile_overrides[sel.dataset.slot] = sel.value; else delete f.advanced.profile_overrides[sel.dataset.slot];
    }));
    on("verify", async (e) => {
      e.target.disabled = true;
      document.getElementById("verify-status").textContent = "Sprawdzam… (zwykle kilka–kilkanaście sekund na model)";
      try {
        const r = await api("/api/models/verify", setupPayload());
        boot.providers = r.providers; f.setup = r.setup;
        const res = r.results.map((x) => `${x.model}: ${{ACCEPTED: "OK", REJECTED: "odrzucony", UNKNOWN: "nie ustalono"}[x.status]}`).join(", ");
        toast(res ? `Wynik: ${res}` : "Brak modeli do sprawdzenia.", 9000);
      } catch (err) { toast(err.message, 8000); }
      draw(); refreshNext();
    });
    refreshNext();
  };
  try { await loadSetup(); } catch (e) { box.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  draw();
}
function chainActive() {
  const c = formState.setup && formState.setup.implementer_chain;
  return !!(formState.implementer_chain || (c && c.source !== "SLOTS"));
}
function advancedSlotsHtml() {
  const profiles = [];
  for (const p of (boot.providers?.providers || [])) for (const r of p.profiles || []) if (r.profile_id) profiles.push(r);
  const f = formState;
  const opts = (slot) => `<option value="">(wg poziomu)</option>` + profiles.map((p) => `<option value="${esc(p.profile_id)}" ${f.advanced.profile_overrides[slot] === p.profile_id ? "selected" : ""}>${esc(p.profile_id)} — ${esc(p.runtime_model_id || "brak ID")} / ${esc(p.effort)}${p.runnable ? "" : " (niedostępny)"}</option>`).join("");
  return `<p class="muted">Tylko dla zaawansowanych: wymusza dokładny profil dla etapu. Niedostępny profil zablokuje START albo zatrzyma run w Human Gate — nigdy nie zostanie podmieniony.</p>
    ${chainActive() ? `<p class="small muted">Etapy implementacji i napraw wynikają z łańcucha implementatora powyżej, dlatego nie można ich tu nadpisać osobno.</p>` : ""}
    <table>${Object.entries(boot.slot_labels).filter(([slot]) => !(chainActive() && CHAIN_SLOTS.includes(slot))).map(([slot, label]) => `<tr><td>${esc(label)}</td><td><select data-slot="${esc(slot)}">${opts(slot)}</select></td></tr>`).join("")}</table>`;
}

function stepGoal(box) {
  const f = formState;
  box.innerHTML = `<h2>4. Co chcesz osiągnąć?</h2>
    <p class="hint">Opisz cel całego zadania własnymi słowami — tak, jak powiedziałbyś to współpracownikowi.</p>
    <textarea id="goal" rows="4" placeholder="np. Prosta aplikacja do śledzenia wydatków z eksportem do CSV">${esc(f.goal)}</textarea>
    <div class="examples"><span class="small muted">Przykłady (kliknij, aby wypełnić kroki 4–6):</span>
      ${EXAMPLES.map((x, i) => `<button class="link" data-example="${i}">${esc(x.title)}</button>`).join("")}</div>`;
  const t = document.getElementById("goal");
  t.addEventListener("input", () => { f.goal = t.value; refreshNext(); });
  box.querySelectorAll("[data-example]").forEach((b) => (b.onclick = () => {
    const x = EXAMPLES[Number(b.dataset.example)];
    f.goal = x.goal; f.first_iteration = x.first; f.directions = x.dirs; t.value = x.goal; refreshNext();
    toast("Wypełniono cel, pierwszą iterację i kierunek przykładem — możesz je zmienić.");
  }));
  t.focus();
}
function stepFirst(box) {
  const f = formState;
  box.innerHTML = `<h2>5. Pierwsza iteracja <span class="hint">(opcjonalnie)</span></h2>
    <p class="hint">Konkretny, mały zakres na start. Zostaw puste, a AAW zacznie od celu z kroku 4.</p>
    <textarea id="first" rows="4" placeholder="np. Model danych wydatku i zapis do pliku JSON z testami">${esc(f.first_iteration)}</textarea>`;
  const t = document.getElementById("first");
  t.addEventListener("input", () => { f.first_iteration = t.value; });
  t.focus();
}
function stepDirections(box) {
  const f = formState;
  box.innerHTML = `<h2>6. Dalszy kierunek <span class="hint">(opcjonalnie, każdy punkt w osobnej linii)</span></h2>
    <div class="note small">Podaj <strong>szerokie kierunki</strong>, nie szczegółowy plan. Im dalszy etap, tym mniej wiadomo o jego dokładnej formie — AAW zaplanuje szczegóły, gdy do nich dojdzie, i zatrzyma się, gdy zabraknie punktów.</div>
    <textarea id="dirs" rows="5" placeholder="- interfejs w terminalu&#10;- eksport do CSV&#10;- raport miesięczny">${esc(f.directions)}</textarea>`;
  const t = document.getElementById("dirs");
  t.addEventListener("input", () => { f.directions = t.value; });
  t.focus();
}
function formPayload() {
  const f = formState, a = f.advanced;
  const lines = (t) => String(t || "").split("\n").map((x) => x.trim()).filter(Boolean);
  return {repo: f.repo, goal: f.goal, first_iteration: f.first_iteration, directions: lines(f.directions),
          planning: f.planning, implementation: f.implementation, review: f.review, implementer_chain: f.implementer_chain, base: f.base,
          advanced: {acceptance_criteria: lines(a.acceptance_criteria), required_evidence: lines(a.required_evidence),
                     forbidden_areas: lines(a.forbidden_areas), max_iterations: a.max_iterations ? Number(a.max_iterations) : null,
                     max_repair_attempts: a.max_repair_attempts ? Number(a.max_repair_attempts) : null,
                     profile_overrides: a.profile_overrides}};
}
function slotRow(s) {
  return `<tr><td>${esc(s.label)}</td><td>${slotLine({...s, label: ""})}<span class="muted small mono">${esc(s.profile_id)} · ${esc(s.runtime_model_id || "")} / ${esc(s.effort || "")}</span></td></tr>`;
}
async function stepStart(box) {
  const f = formState;
  box.innerHTML = `<h2>7. Podsumowanie i START</h2><div class="muted">Przygotowuję podsumowanie…</div>`;
  let p;
  try { p = await api("/api/tasks/preview", {form: formPayload()}); }
  catch (e) { box.innerHTML = `<h2>7. Podsumowanie i START</h2><div class="note bad">${esc(e.message)}</div>`; return; }
  f.preview = p;
  const g = p.groups;
  const models = (key, name) => `<tr><td>${name}</td><td>${g[key].models.map(esc).join(key === "implementation" && p.implementer_chain.source !== "SLOTS" ? " → " : ", ")}${g[key].alternatives.length ? ` <span class="slot-status ALTERNATIVE">${g[key].alternatives.length} alternatyw</span>` : ""}</td></tr>`;
  const a = f.advanced;
  box.innerHTML = `<h2>7. Podsumowanie i START</h2>
    ${p.blockers.map((b) => `<div class="note bad"><strong>Nie można wystartować:</strong> ${esc(b)}</div>`).join("")}
    ${p.warnings.map((w) => `<div class="note warn small">${esc(w)}</div>`).join("")}
    <table class="summary-table">
      <tr><td>Projekt</td><td><strong>${esc(p.project.name || "")}</strong>${p.project.base ? " (kontynuacja)" : ""}</td></tr>
      <tr><td>Cel</td><td>${esc(p.goal)}</td></tr>
      <tr><td>Pierwsza iteracja</td><td>${esc(p.first_iteration)}</td></tr>
      <tr><td>Kierunek (roadmapa)</td><td><ol style="margin:0;padding-left:18px">${p.roadmap.map((r) => `<li>${esc(r.title)}</li>`).join("")}</ol></td></tr>
      ${models("planning", "Planowanie")}${models("implementation", "Implementacja")}${models("review", "Review")}
      <tr><td>Limity</td><td>maks. ${esc(p.limits.max_iterations)} iteracji · maks. ${esc(p.limits.max_repair_attempts)} napraw na iterację</td></tr>
    </table>
    <ul class="safety">${p.safety.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
    <details style="margin-top:12px"><summary>Zaawansowane</summary>
      <label class="field">Kryteria akceptacji pierwszej iteracji <span class="hint">(po jednym w linii; puste = domyślne)</span></label>
      <textarea id="acc">${esc(a.acceptance_criteria)}</textarea>
      <label class="field">Wymagane dowody <span class="hint">(np. "unit tests")</span></label>
      <textarea id="ev">${esc(a.required_evidence)}</textarea>
      <label class="field">Obszary zabronione <span class="hint">(ścieżki, po jednej w linii)</span></label>
      <textarea id="forb">${esc(a.forbidden_areas)}</textarea>
      <div class="inline" style="margin-top:12px"><label class="small">Maks. iteracji <input type="number" id="maxit" min="1" max="50" value="${esc(a.max_iterations)}"></label>
      <label class="small">Maks. napraw na iterację <input type="number" id="maxrep" min="1" max="6" value="${esc(a.max_repair_attempts)}"></label>
      <button id="apply-adv">Zastosuj</button></div>
      <h3 style="margin-top:16px">Wszystkie etapy i dokładne profile</h3>
      <table class="summary-table">${slotRow(p.planner)}${Object.values(p.implementer_policy).map(slotRow).join("")}${Object.values(p.review_policy).map(slotRow).join("")}</table>
      <div class="small muted">Katalog rekomendacji ${esc(p.catalog.version)} (${esc(p.catalog.source)}). Projekt: <span class="mono">${esc(p.project.path || "")}</span>${p.project.branch ? ` · ${esc(p.project.branch)} @ ${esc((p.project.head || "").slice(0, 10))}` : ""}</div>
    </details>
    <div class="actions start-row"><button class="primary start-btn" id="start" ${p.can_start ? "" : "disabled"}>START</button>
      <span class="small muted">${p.can_start ? "AAW zacznie pracę w tle. Możesz zamknąć to okno — praca trwa dalej." : "Popraw powyższe, aby wystartować."}</span></div>`;
  on("apply-adv", () => {
    a.acceptance_criteria = val("acc"); a.required_evidence = val("ev"); a.forbidden_areas = val("forb");
    a.max_iterations = val("maxit"); a.max_repair_attempts = val("maxrep"); stepStart(box);
  });
  on("start", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/tasks/start", {form: formPayload()});
      formState = null;
      location.hash = `#/runs/${r.run_id}`;
    } catch (err) { toast(err.message, 9000); e.target.disabled = false; }
  });
}

// ── run detail ───────────────────────────────────────────────────────────────
const MARK = {done: "✓", active: "●", stopped: "❚❚", pending: "○", failed: "✕"};
function bannerHtml(v) {
  const p = v.process || {};
  const cls = {RUNNING: "run", STARTING: "run", STOPPING: "stop", PAUSED: "pause", INTERRUPTED: "pause", READY_FOR_DECISION: "gate",
               NEEDS_ATTENTION: "bad", START_FAILED: "bad", ACCEPTED: "ok", REJECTED: "muted"}[v.status] || "muted";
  const where = p.iteration ? `Iteracja ${esc(p.iteration)}${p.phase_label ? " · " + esc(p.phase_label) : ""}` : "";
  return `<div class="banner ${cls}"><div class="banner-status">${esc(v.status_label)}</div><div class="banner-where">${where}</div></div>`;
}
function processHtml(v) {
  const p = v.process;
  if (!p) {
    const failed = v.status === "START_FAILED";
    return `<div class="panel"><div class="${failed ? "note bad" : "muted"}">${failed ? "Nie udało się wystartować: " + esc(v.status_detail || (v.worker_exit || {}).error || "") : "Uruchamianie procesu AAW…"}</div>${controlsHtml(v)}</div>`;
  }
  const steps = p.nodes.map((n) => `<li class="${n.state}"><span class="mark">${MARK[n.state]}</span>${esc(n.label)}${n.count > 1 ? ` <span class="count">×${n.count}</span>` : ""}</li>`).join("");
  const running = ["RUNNING", "STOPPING"].includes(v.status);
  const current = p.activity || (v.gate ? "Czeka na Twoją decyzję (Human Gate)" : v.status_label);
  return `<div class="panel"><div class="process">
    <div><div class="iter-title">ITERACJA ${esc(p.iteration)}</div><ul class="steps">${steps}</ul></div>
    <div class="now">
      <div class="k-label">Aktualnie</div>
      <div class="big">${esc(current)}</div>
      <div class="stat-row">
        <div class="stat"><div class="k">Model</div><div class="v">${esc(p.model || "—")}</div></div>
        <div class="stat"><div class="k">Ten krok</div><div class="v">${running ? fmtDuration(p.step_elapsed_s ?? p.phase_elapsed_s) : "—"}</div></div>
        <div class="stat"><div class="k">Czas całkowity</div><div class="v">${fmtDuration(p.run_elapsed_s)}</div></div>
        <div class="stat"><div class="k">Roadmapa</div><div class="v">${esc(p.roadmap.done)} / ${esc(p.roadmap.total)}</div></div>
      </div>
      <div class="small muted" style="margin-top:8px">AAW nie pokazuje szacowanego czasu zakończenia — nie ma danych, by podać go uczciwie.</div>
      ${v.status_detail ? `<div class="note warn" style="margin-top:12px">${esc(v.status_detail)}</div>` : ""}
      ${stopInfo(v)}
    </div></div>
    ${controlsHtml(v)}</div>`;
}
function stopInfo(v) {
  const ev = ((v.stop_effect || {}).events || []).slice(-1)[0];
  if (v.status !== "STOPPING" || !ev) return v.status === "STOPPING" ? `<div class="note warn" style="margin-top:12px">Żądanie zatrzymania wysłane…</div>` : "";
  const text = {PAUSE_AT_NEXT_BOUNDARY: "Bieżący krok zmienia pliki lub jest jednorazowym planem — przerwanie dałoby niepewny stan. AAW dokończy go i zatrzyma się zaraz potem.",
                CANCEL_READ_ONLY_CALL: "Przerwano krok tylko do odczytu; po wznowieniu zostanie powtórzony.",
                FORCE: "STOP NOW: kończę procesy AAW i providera."}[ev.mode] || ev.mode;
  return `<div class="note warn" style="margin-top:12px">${esc(text)}</div>`;
}
function controlsHtml(v) {
  const c = v.controls, out = [];
  if (c.stop) out.push(`<div class="ctl"><button class="stop" id="btn-stop">STOP SAFELY</button><div class="ctl-help">Bezpiecznie: nie zaczyna kolejnego etapu. Krok zmieniający pliki zostaje dokończony, krok tylko do odczytu jest przerywany i powtórzony po wznowieniu.</div></div>`);
  if (c.force_stop) out.push(`<div class="ctl"><button class="stop-now" id="btn-force">STOP NOW</button><div class="ctl-help">Natychmiast: kończy proces AAW i providera. Jeśli trwała implementacja lub naprawa, jej skutek jest niepewny — po wznowieniu AAW nie powtórzy jej w ciemno, tylko poprosi o decyzję.</div></div>`);
  if (c.resume) out.push(`<div class="ctl"><button class="primary resume" id="btn-resume">RESUME</button><div class="ctl-help">Wznawia od zapisanego miejsca. Krok o niepewnym skutku nigdy nie jest powtarzany automatycznie.</div></div>`);
  return out.length ? `<div class="controls">${out.join("")}</div>` : "";
}
function briefHtml(b) {
  const list = (items) => items.length ? `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<span class="muted">—</span>`;
  const checks = b.checks.length ? `<ul>${b.checks.map((c) => `<li><span class="chk ${esc(c.status)}">${esc(c.status)}</span> ${esc(c.name)}${c.summary ? " — " + esc(c.summary) : ""}</li>`).join("")}</ul>` : `<span class="muted">—</span>`;
  return `<div class="brief ${esc(b.kind)}"><h4><span>${esc(b.phase)}</span><span class="muted small">${esc(fmtTime(b.at))}</span></h4>
    <div class="bgrid">
      <div class="bk">Model</div><div>${esc(b.model)}</div>
      <div class="bk">Zadanie etapu</div><div>${b.goal ? esc(b.goal) : `<span class="muted">—</span>`}</div>
      <div class="bk">Co zrobiono</div><div>${list(b.done)}</div>
      <div class="bk">Testy / checki</div><div>${checks}</div>
      <div class="bk">Problemy / niepewności</div><div>${list(b.problems)}</div>
      <div class="bk">Przekazano dalej</div><div>${list(b.handed_over)}</div>
    </div>
    ${b.execution_id ? `<div style="margin-top:6px"><button class="link" data-evidence="${esc(b.execution_id)}">View raw evidence (surowe dowody)</button> <span class="muted small mono adv-only">${esc(b.execution_id)} · ${esc(b.ledger_state || "")}</span></div>` : ""}
  </div>`;
}
function timelineHtml(v) {
  if (!v.timeline.length) return "";
  return `<h2>Przebieg (briefy etapów)</h2><p class="small muted">Brief to skrót zapisanych dowodów — źródłem prawdy są surowe dowody silnika.</p>` + v.timeline.map((t) => {
    const cls = t.label.endsWith("PASS") ? "PASS" : /ESKALACJA|PRZERWANA/.test(t.label) ? "bad" : "prog";
    const key = t.iteration_id || "RUN";
    return `<details class="iter" data-iter="${esc(key)}" ${openIterations.has(key) ? "open" : ""}><summary>
      <strong>${t.index ? "Iteracja " + esc(t.index) : "Zdarzenia"}</strong><span class="label ${cls}">${esc(t.label)}</span>
      <span class="muted small">${esc(t.goal || "")}</span></summary>
      <div class="briefs">${t.briefs.length ? t.briefs.map(briefHtml).join("") : `<div class="muted small">Brak zakończonych etapów.</div>`}</div></details>`;
  }).join("");
}
function gateHtml(v) {
  const g = v.gate;
  if (!g) return "";
  const list = (items, empty) => items.length ? `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="muted small">${empty}</div>`;
  const c = g.candidate, a = g.actions;
  const decided = g.decision ? `<div class="note ${g.decision.decision === "ACCEPTED" ? "ok" : ""}"><strong>Decyzja: ${esc(g.decision.decision)}</strong> (${esc(fmtTime(g.decision.at))})${g.decision.integration ? ` · <span class="mono">${esc(g.decision.integration.status)}</span> (merge: ${g.decision.integration.merged ? "tak" : "nie"}, push: ${g.decision.integration.pushed ? "tak" : "nie"})` : ""}${(g.decision.next_steps || []).map((s) => `<br>${esc(s)}`).join("")}</div>` : "";
  return `<div class="panel gate"><h2>Human Gate</h2>${decided}
    <div class="sec"><h3>Dlaczego AAW się zatrzymał</h3><div class="why">${esc(g.why)}</div></div>
    <div class="sec"><h3>Co zrobiono</h3>${list(g.done, "Żadna iteracja nie została zaakceptowana.")}</div>
    <div class="sec"><h3>Co zostało</h3>${list(g.remaining, "Nic — roadmapa wyczerpana.")}</div>
    <div class="sec"><h3>Ostrzeżenia</h3>${list(g.warnings, "Brak.")}</div>
    <div class="sec"><h3>Bieżący kandydat</h3><div class="kv small">
      <div>Zmienione pliki</div><div class="mono">${esc((c.changed_files || []).join(", ") || "—")}</div>
      ${c.last_repair ? `<div>Ostatnia naprawa</div><div class="small">${c.last_repair.code_changed ? "zmieniła kod" : "bez zmian w kodzie (cała iteracja — lista powyżej)"}${(c.last_repair.signals || []).length ? " · postęp: " + esc(c.last_repair.signals.join(", ")) : ""}</div>` : ""}
      <div>Folder z wynikiem</div><div class="mono">${esc(c.worktree)}</div>
      <div>Gałąź</div><div class="mono">${esc(c.branch)}</div>
      <div>Można zaakceptować</div><div>${c.promotable ? "tak" : "nie (eskalacja lub brak zaakceptowanej iteracji)"}</div>
      <div class="adv-only">Kandydat / HEAD</div><div class="mono adv-only">${esc(c.candidate_id || "—")} · ${esc(c.head || "—")}</div>
    </div><div class="small muted" style="margin-top:6px">${esc(g.merge_push)} <span class="mono adv-only">main_merge_allowed = ${esc(String(g.main_merge_allowed))}</span></div></div>
    <div class="gate-actions">
      ${a.accept ? `<div class="ctl"><button class="primary" id="g-accept">Akceptuj</button><div class="ctl-help">${esc(g.accept_meaning)}</div></div>` : ""}
      ${a.add_direction ? `<div class="ctl"><button id="g-dir">Dodaj kierunek</button><div class="ctl-help">Nowe zadanie z tym samym celem, startujące od tego wyniku.</div></div>` : ""}
      ${a.new_goal ? `<div class="ctl"><button id="g-goal">Kontynuuj z nowym celem</button><div class="ctl-help">Nowe zadanie z nowym celem, od tego wyniku.</div></div>` : ""}
      ${a.reject ? `<div class="ctl"><button class="danger" id="g-reject">Odrzuć</button><div class="ctl-help">Zamyka zadanie; kopia robocza zostaje do wglądu.</div></div>` : ""}
      <div class="ctl"><button id="g-evidence">Pokaż dowody</button><div class="ctl-help">Surowe artefakty silnika.</div></div>
    </div></div>`;
}
function technicalHtml(v) {
  const w = v.workspace || {}, e = v.engine || {};
  const slots = Object.values((v.resolution || {}).slots || {});
  return `<details id="tech"><summary>Zaawansowane: szczegóły techniczne</summary><div class="panel" style="margin-top:10px"><div class="kv small">
    <div>Run ID</div><div class="mono">${esc(v.run_id)}</div>
    <div>Status silnika</div><div class="mono">${esc(e.status)} / ${esc(e.phase)} · ${esc(e.contract || "")} · ${esc(e.policy_preset || "")}</div>
    <div>Repozytorium</div><div class="mono">${esc(w.repo)}</div>
    <div>Worktree (izolowana kopia)</div><div class="mono">${esc(w.worktree)}</div>
    <div>Gałąź / baza</div><div class="mono">${esc(w.branch)} @ ${esc((w.base_commit || "").slice(0, 12))}</div>
    <div>Bieżące wywołanie (execution_id)</div><div class="mono">${esc((v.process || {}).execution_id || "—")}</div>
    <div>Blokada runu</div><div class="mono">${esc(JSON.stringify((v.controls || {}).lock_token || null))}</div>
  </div><table style="margin-top:12px">${slots.map((s) => `<tr><td>${esc(s.label)}</td><td class="mono">${esc(s.profile_id)} · ${esc(s.runtime_model_id || "")}/${esc(s.effort || "")} · ${esc(s.status)}${s.exact_mapping_of ? " (dokładne mapowanie " + esc(s.exact_mapping_of) + ")" : ""}</td></tr>`).join("")}</table>
  <div class="actions"><button id="raw-all">Surowe dowody runu (ledger, dziennik, stan)</button></div></div></details>`;
}
async function showEvidence(runId, executionId) {
  const q = executionId ? `?execution_id=${encodeURIComponent(executionId)}` : "";
  const data = await api(`/api/runs/${runId}/evidence${q}`);
  modal(executionId ? `Surowe dowody: ${executionId}` : "Surowe dowody runu",
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
    const techOpen = document.getElementById("tech")?.open;
    view.innerHTML = `<div class="muted small"><a href="#/home">Home</a> › ${esc(v.project || "")}</div>
      <h1 class="run-title">${esc(v.goal)}</h1>${bannerHtml(v)}
      ${gateHtml(v)}${processHtml(v)}${timelineHtml(v)}${technicalHtml(v)}`;
    const tech = document.getElementById("tech");
    if (techOpen) tech.open = true;
    document.body.classList.toggle("show-adv", !!techOpen);
    tech.addEventListener("toggle", () => document.body.classList.toggle("show-adv", tech.open));
    bindRun(runId, v);
    const active = ["RUNNING", "STOPPING", "STARTING"].includes(v.status);
    setPoll(() => draw().catch(() => {}), active ? 1500 : 5000);
  };
  await draw();
}
function bindRun(runId, v) {
  const act = async (path, body, msg) => {
    try {
      const r = await api(`/api/runs/${runId}/${path}`, body);
      if (msg) toast(msg);
      if (path !== "continue") setTimeout(() => { if (location.hash === `#/runs/${runId}`) route(); }, 300);
      return r;
    }
    catch (e) { toast(e.message, 8000); }
  };
  on("btn-stop", () => act("stop", {force: false}, "STOP SAFELY: AAW nie zacznie kolejnego etapu."));
  on("btn-force", () => {
    if (confirm("STOP NOW — zatrzymać natychmiast?\n\nJeśli trwa implementacja lub naprawa, jej skutki w kopii roboczej będą NIEPEWNE. Po wznowieniu AAW nie powtórzy jej automatycznie, tylko poprosi Cię o decyzję.\n\nJeśli wystarczy zatrzymanie po bieżącym kroku, wybierz Anuluj i użyj STOP SAFELY.")) act("stop", {force: true}, "STOP NOW: zatrzymywanie.");
  });
  on("btn-resume", () => act("resume", {lock_token: v.controls.lock_token}, "Wznawianie…"));
  on("g-accept", async () => {
    const early = v.gate.actions.accept_needs_early_end;
    if (early && !confirm("Roadmapa nie jest wyczerpana. Zaakceptować wynik mimo to (zakończyć wcześniej)?\n\nAAW nie zrobi merge ani push.")) return;
    if (!early && !confirm("Zaakceptować ten wynik?\n\nStatus: READY_FOR_EXTERNAL_INTEGRATION. AAW nie zrobi merge ani push — zintegrujesz zmiany sam, gdy zechcesz.")) return;
    await act("accept", {early_end: early}, "Zaakceptowano: READY_FOR_EXTERNAL_INTEGRATION. Nic nie zostało zmergowane ani wypchnięte.");
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
    const msg = (mode === "direction" ? "Dodać kierunek? " : "Kontynuować z nowym celem? ") + base;
    if (!confirm(msg)) return;
    const r = await act("continue", {mode});
    if (!r) return;
    formState = Object.assign(defaultForm(), r.prefill);
    formState.directions = (r.prefill.directions || []).join("\n");
    formState.planning = r.prefill.planning || boot.settings.planning;
    formState.implementation = r.prefill.implementation || boot.settings.implementation;
    formState.review = r.prefill.review || boot.settings.review;
    formState.step = mode === "direction" ? 5 : 3;  // jump to the direction / goal step; earlier steps stay valid
    if (!r.prefill.base) { formState.step = 0; }
    await loadSetup().catch(() => {});
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
  const s = boot.settings;
  const sel = (group) => `<select data-setting="${group}">${boot.choices[group].options.map((o) => `<option value="${esc(o.value)}" ${s[group] === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
  view.innerHTML = `<h1>Ustawienia</h1><p class="lead">${esc(boot.notice)}</p>
    <div class="panel"><h3>Narzędzia AI (CLI)</h3><p class="hint">${esc(boot.setup_notice)}</p>
      <div id="prov">${providersHtml(boot.providers)}</div>
      <div class="actions"><button class="primary" id="redetect">Wykryj ponownie</button><button id="verify-all">Sprawdź modele</button></div>
      <details class="small"><summary>Wszystkie profile AAW i ich dostępność</summary>${profileTable()}</details></div>
    <div class="panel"><h3>Domyślne poziomy dla nowych zadań</h3><div class="kv">
      <div>Planowanie</div><div>${sel("planning")}</div>
      <div>Implementacja</div><div>${sel("implementation")}</div>
      <div>Review</div><div>${sel("review")}</div>
      <div>Aktualizacje rekomendacji online</div><div><label><input type="checkbox" id="online" ${s.online_recommendation_updates ? "checked" : ""}> sprawdzaj przy starcie (tylko odczyt pliku z GitHub AAW)</label></div>
      <div>Katalog rekomendacji</div><div>${esc(boot.catalog.version)} (${esc(boot.catalog.source)}) <button class="link" id="upd">Sprawdź aktualizację teraz</button></div>
    </div><div class="actions"><button class="primary" id="save">Zapisz</button></div></div>
    <details><summary>Zaawansowane</summary><div class="panel" style="margin-top:10px">
      <div class="kv small"><div>Folder danych</div><div class="mono">${esc(boot.data_home)}</div>
      <div>Wersja</div><div>${esc(boot.release.name)} (${esc(boot.release.release)})${boot.frozen ? " · portable" : " · źródła"}</div>
      <div>Silnik</div><div>${esc(boot.release.engine)}</div></div>
      <div id="recs"></div>
      <div class="actions"><button id="show-recs">Pokaż katalog rekomendacji</button><button id="welcome-again">Pokaż ekran powitalny</button>
      ${boot.control_center_available ? `<button id="cc">Otwórz konsolę operatora (Control Center)</button>` : ""}</div>
    </div></details>
    <div class="panel" style="margin-top:16px"><h3>Zamknij AAW</h3><p class="small muted">Zamyka aplikację AAW. Trwające zadania pracują dalej w tle; po ponownym uruchomieniu AAW zobaczysz ich stan na Home.</p>
      <button class="danger" id="quit">Zamknij AAW</button></div>`;
  on("redetect", async () => { await detectNow(); renderSettings(); });
  on("verify-all", async (e) => {
    e.target.disabled = true; toast("Sprawdzam modele… (jedno krótkie zapytanie na model)", 9000);
    try {
      const r = await api("/api/models/verify", {});
      boot.providers = r.providers;
      toast(r.results.length ? r.results.map((x) => `${x.model}: ${x.status}`).join(", ") : "Brak modeli do sprawdzenia.", 9000);
    } catch (err) { toast(err.message, 8000); }
    renderSettings();
  });
  on("save", async () => {
    const body = {online_recommendation_updates: document.getElementById("online").checked};
    view.querySelectorAll("[data-setting]").forEach((el) => (body[el.dataset.setting] = el.value));
    try { boot.settings = await api("/api/settings", body); toast("Zapisano."); } catch (e) { toast(e.message); }
  });
  on("upd", async () => {
    const r = await api("/api/recommendations/update", {});
    toast(`${r.status}: ${r.detail}`, 7000);
    boot = await api("/api/bootstrap");
  });
  on("welcome-again", () => { formState = defaultForm(); formState.step = -1; location.hash = "#/new"; route(); });
  on("show-recs", async () => {
    const r = await api("/api/recommendations");
    document.getElementById("recs").innerHTML = `<table style="margin-top:10px"><tr><th>Profil</th><th>Provider</th><th>Role</th><th>Siła/szybkość/koszt</th><th>Status</th><th>Tutaj</th></tr>${r.profiles.map((p) => `<tr><td class="mono">${esc(p.profile)}</td><td>${esc(p.provider)}</td><td class="small">${esc(p.recommended_roles.join(", "))}</td><td class="small">${esc(p.strength_class)} / ${esc(p.speed_class)} / ${esc(p.cost_class)}</td><td class="small">${esc(p.status)}</td><td>${p.runnable_here ? "✓" : "✕"}</td></tr>`).join("")}</table>`;
  });
  on("cc", async () => { try { await api("/api/advanced/control-center", {}); toast("Uruchomiono Control Center."); } catch (e) { toast(e.message); } });
  on("quit", async () => {
    if (!confirm("Zamknąć AAW? Trwające zadania będą kontynuowane w tle.")) return;
    const r = await api("/api/quit", {});
    view.innerHTML = `<div class="panel"><h2>AAW zamknięty</h2><p>${esc(r.note)}</p><p class="muted">Możesz zamknąć tę kartę. Aby wrócić, uruchom ponownie AAW.exe.</p></div>`;
    setPoll(null);
  });
}
function profileTable() {
  const rows = [];
  for (const p of (boot.providers?.providers || [])) for (const r of p.profiles || []) if (r.profile_id) rows.push({...r, provider: p.display_name});
  return `<table><tr><th>Profil</th><th>CLI</th><th>Model / effort</th><th>Dostępność</th></tr>${rows.map((r) => `<tr><td class="mono">${esc(r.profile_id)}</td><td>${esc(r.provider)}</td><td class="mono">${esc(r.runtime_model_id || "—")} / ${esc(r.effort)}</td><td><span class="chk ${STATE_CLASS[r.availability] || ""}">●</span> ${esc(r.availability_label || "")}${r.availability_reason ? `<div class="muted">${esc(r.availability_reason)}</div>` : ""}</td></tr>`).join("")}</table>`;
}

// ── boot ─────────────────────────────────────────────────────────────────────
(async function start() {
  try {
    boot = await api("/api/bootstrap");
  } catch (e) {
    view.innerHTML = `<div class="note bad">Nie można połączyć się z AAW: ${esc(e.message)}</div>`;
    return;
  }
  document.getElementById("nav-foot").textContent = `AAW ${boot.release.release}`;
  if (!boot.providers) {
    try { boot.providers = await api("/api/providers/detect", {}); } catch (e) { /* shown on Home */ }
  }
  if (boot.settings.online_recommendation_updates) {
    api("/api/recommendations/update", {}).then(async (r) => { if (r.status === "UPDATED") { toast("Zaktualizowano katalog rekomendacji (dotyczy nowych zadań)."); boot = await api("/api/bootstrap"); } }).catch(() => {});
  }
  if (!boot.settings.first_run_completed && !boot.has_runs && !location.hash) location.hash = "#/new";
  route();
})();
