# AAW Real-time Experience V0.1 — a live run, a benchmark that builds itself, and an honest "what would be optimal"

Status: surfaces 1–4 below and the three closing-the-loop features of §7a (recommended-model button, opt-in exploration,
idea intake) are implemented and tested (`aaw_experience.py`, `autonomy_explore.py`, `product_intake.py`, `product_view.py`,
`PRODUCT_UI/experience.js`); the remaining items in §7 are designed, not built. Builds on `AAW_OPTIMIZATION_PROJECT_V0_1.md`
(chain mode, telemetry V1).

## 1. What changed in the thinking

The first version of the optimization project ended with "run the benchmark in the lab" — an artificial workflow the
user would have to start, pay for, and remember. That is the wrong shape. The rule now:

> **Nothing the user must run. Every real iteration is a data point.** The product records evidence anyway (engine state +
> `telemetry.jsonl`); the benchmark, the optimum, the forecast and the "how far can this go" answer are *derived from it,
> on demand, read-only* and shown where the user already looks. `BENCH/` (the lab suite) is demoted to an optional seed for
> a cold start.

This is also how one benchmark can serve different people: it is **per task kind**, and the overall choice is weighted by
the user's own mix of kinds. A person who mostly fixes bugs and a person who mostly writes documentation see different charts
and different optimums from the same code (see the "Przykład" preview, personas *backend / web / researcher*).

## 2. The four surfaces

| # | Surface | Where | What it answers |
|---|---|---|---|
| 1 | **Live run** | run page, above the old process panel | Where is this run on the whole path *right now*, what is it costing, how much is left? |
| 2 | **Benchmark ("Doświadczenie")** | new nav item | Which model is cheapest *per solved task* for **my** kinds of work? |
| 3 | **Optimum** | beside the benchmark, before START, at the gate | What would have been (or would be) the better choice — and how sure are we? |
| 4 | **Reach** | benchmark page + live panel | How far do runs like this actually get without a human? |

### 2.1 Live run (real time)
* **Journey strip** — the path from idea to decision: *Pomysł i mandat → Plan początkowy → Łańcuchy implementacji → Review serii →
  Polerowanie → Twoja decyzja*, each `done / active / pending / failed`. After an escalation the stage where the run stopped is
  marked failed instead of pretending the later stages ran.
* **Chain slots** — one square per iteration of the current chain (`accepted ✓`, `provisional ◐` = done but waiting for the serious
  review, `active ●`, `pending`), the last slot flagged "review serii". Hover shows goal and cost.
* **Cost meter** — cost so far (USD list-price equivalent when every call is priced, else proxy units — and it says which),
  calls, tokens in/out, cache share, model time, and a stacked bar of *where the cost goes* (planning / implementation /
  verification / review). The old finding "review is most of the bill" is now visible on every run.
* **Now line** — current step, model, and an elapsed timer that ticks every second **without refetching**. The cost of an
  in-flight call is unknown until it ends; the UI says so instead of guessing.
* **Forecast ("Ile jeszcze?")** — iterations left × the median (p25–p75) iteration cost and time, from *this run* after two
  iterations, else from the user's history of the same kind (≥3), else **nothing, with the reason**. The old UI line "no data to
  estimate honestly" is now this rule. Open-ended runs (standing `CONTINUE` item) say that the end is the planner's decision.
* **Feed** — the last 14 human-readable events (phase completed, iteration provisionally accepted, findings deferred, chain accepted…).
* Polling stays at 1.5 s while running (5 s otherwise); the page only repaints when the payload changed.

### 2.2 The benchmark that builds itself
* **Rows**: one per real iteration — kind, profile, outcome, implementation cost, all-in cost (a chain-closing review is shared
  by the iterations of its chain), wall time, repairs. `solved` = accepted; escalated/abandoned = failed; **still-open or
  provisional-but-never-closed = neither** (it must not flatter or punish a model).
* **Task kind** is classified deterministically from the iteration goal, acceptance criteria and touched paths
  (bug fix, feature, refactor, tests, docs, UI, build/config, analysis, other; Polish + English keywords, path hints,
  confidence). Nothing to configure; planner-declared kinds and user correction are in §7.
* **The chart** — a scatter in a coordinate system: **x = cost per solved task** (log scale when the spread is large),
  **y = solve rate**, bubble size = number of trials, vertical whisker = 95% interval, dashed outline = too few trials,
  dashed line = Pareto front, shaded corner "↖ cheaper and more reliable", ★ = best choice. Tabs: one per kind with data
  plus **"Wszystkie (Twój miks)"**, which weights each model by the user's own mix and shows only models covering ≥50% of it;
  until one does, the screen opens the best-populated kind and says why. Hover/focus gives the numbers; labels never overlap.
* **Cost scope** toggle: implementation + repairs only, or all-in (planning, verification, review).
* **Cold start**: an explicit, clearly labelled "PODGLĄD NA DANYCH PRZYKŁADOWYCH" with three personas — synthetic, never mixed with
  real rows, and the only place a number is not the user's.

### 2.3 What would be optimal (and how sure we are)
Plain-language statements, each with its evidence and confidence (`wiarygodna` n≥8, `wstępna` n≥3, otherwise silent):
* **SWITCH** — "for *Bugs*, X is 62% cheaper per solved task than Y at comparable reliability" — only if X is ≥10% cheaper **and**
  its solve rate is within 5 points of Y's; never because two lucky runs made a model look cheap (a profile with <3 trials can
  neither dominate nor be dominated).
* **KEEP** — the current choice is already the best. **COLLECT** — "2 of 3 trials needed; this will appear on its own."
* **Structure tips** — "74% of cost is review: longer chains will save more than changing the model"; "the serious review needed
  repair in 0 of 5 chains: chains can be longer" / "in 3 of 5: shorter chains catch errors earlier".
* **Before START** (wizard summary): the detected kind, the model that works best for that kind *for this user*, the typical
  iteration cost range. Information only — **AAW never switches a model by itself.**
* **At the gate — "Rozliczenie"**: what the run cost and, where the benchmark supports it, what the better choice would have
  saved as a **cautious range** (half to full of the benchmark's estimate), listing which kind/model swap it refers to.
* The overall ★ must be *reliable*, not merely cheap: the cheapest model that solves 55% of the user's work is not "best".

### 2.4 How far can it go: idea → finish in one chain
Two numbers, both measured, neither promised:
* **Horizon (history)** — of finished runs: share that reached the end in one go, hit the iteration fuse, or needed a human; median
  and middle-50% iterations; the most common stop reasons; reach by run size (1–3 / 4–8 / 9+ iterations). Below 3 finished runs
  it says so instead of showing a percentage.
* **Journey (this run)** — the live strip above, so "how far is it from the end" is one glance.

What bounds the horizon today (so the UI can explain a stop): the iteration fuse (default 40), chain length (8) and chain-close
repairs, reviewer `ESCALATE`, repair-ladder exhaustion, human-required roadmap items, provider/quota failures, Git boundary.

## 3. Data flow (zero configuration)

```
controller ──per executor call──▶ AUTONOMY/telemetry.jsonl   (tokens, cost, wall time, chain, category)
controller ──phase boundaries───▶ AUTONOMY/autonomy_state.json (iterations, outcomes, chain, deferred findings)
                       │
   aaw_experience.scan(runs_root)   disposable index, cached by file mtime, broken runs skipped
        ├─ rows_from_state → kind classification, cost attribution, solved/failed/open
        ├─ benchmark / recommendations / horizon / forecast / settlement / live_view
        ▼
   /api/runs/<id> (live + settlement)   /api/experience[?demo=persona]   /api/experience/forecast
```
Old runs without `telemetry.jsonl` are rebuilt from state + RESULTS artifacts. Everything stays on the machine.

## 4. Honesty rules (the product's tone)
1. Every claim shows its sample size; thresholds: compare at n≥3, "reliable" at n≥8, switch only for ≥10% saving.
2. "Don't know" is a valid answer and is worded as a promise ("will appear on its own"), never as a task.
3. Cost basis is named (USD list-price equivalent vs proxy units); prices are marked unverified; subscriptions bill quota, not tokens.
4. The synthetic preview is labelled in the banner and can never be confused with real data.
5. The experience layer cannot hide a run: if it fails, the run page still renders with an error note in its panel.

## 5. Acceptance criteria (all met)
* A clean install shows an empty, explanatory Experience page; seeded runs show per-kind charts with no setup step.
* A run page shows journey, chain slots, cost split and a forecast that is `null` until there is data.
* The benchmark differs between personas given the same engine; ★ respects the reliability floor.
* 47 tests: classifier, row attribution (incl. shared chain-close cost), cache invalidation, broken-run tolerance, benchmark/Pareto/
  mix weighting, recommendations, settlement, horizon, forecast, journey (incl. failed stage), live view, API integration.
* Visually checked in the browser on a seeded home (light theme): chart, personas, live panel, gate settlement.

## 6. Known limits
* `solved` means *accepted by the reviewer*, not "the human liked it" — the human Accept/Reject at the gate is the stronger label (§7).
* Early charts are thin by construction; whiskers and "za mało prób" show that rather than hide it.
* Kind classification is keyword-based: a vaguely worded goal lands in *Inne*.
* A run still in progress cannot show the cost of the call that is currently in flight.
* The live view was verified with a real recorded run re-labelled as running; a true in-flight run needs a provider and was not exercised here.

## 7a. Closing the loop (implemented)

**Recommended model, confirmed by the user.** In the wizard summary the forecast box says which model works best *for this kind
of work, for this user* (cheapest with ≥80% solve rate at n≥3, never on two lucky runs). If it differs from the model that would
be used, and it runs on this machine, a button offers *"Użyj X jako domyślnego modelu implementacji"* with a confirmation that
states the evidence and what it replaces. It sets `profile_overrides.implementer_default` for **this task only**, records an audit
entry (`advanced.recommendation`, `confirmed_by_user`, shown under the run's technical details) and can be undone with one click.
A model that cannot run here is explained, not offered. Nothing is ever applied without the click.

**Opt-in controlled exploration** (`autonomy_explore.py`, settings *Eksploracja*, per-task checkbox; off by default). A small,
deterministic share of *ordinary* iterations is implemented by another model so thin cells of the user's benchmark fill from real
work. Hard rules, all tested: default-implementer slot of a tier-NORMAL iteration only (never repairs, harder tiers, the first
iteration, `critical_scope`, runs with a charter risk floor, kind INFRA); candidates are models this machine runs now, recommended
for implementation, **no more expensive than the default**, never the reviewer's own model when independence is required, and
never one with ≥5 trials and <50% solved; only for kinds whose cell for that candidate has <3 trials; budget = every Nth eligible
iteration (default every 5th, from the 2nd) and ≤3 per run; an unrunnable candidate is skipped (journaled), never turned into an
error. The decision is stored once per iteration (a replay cannot change it), journaled as `EXPLORATION_SELECTED`, recorded as
selection reason `EXPLORATION`, marked **E** on the chain slot and counted on the Experience page ("Zebrano dzięki niej: n prób").

**Idea intake** (`product_intake.py`, panel *"Masz tylko luźny pomysł?"* in the wizard's goal step). One user-initiated, confirmed,
read-only call to the planner profile the run would use (default: the strongest) turns a loose idea into: goal, a smallest-slice
first iteration, an ordered roadmap (kind, size, why, and whether only a human can do it), acceptance criteria, constraints,
forbidden areas, assumptions, **open questions that would change the scope**, risks and a definition of done. The proposal is shown
with its **horizon and cost before START**: iterations, chains (each with one serious review) + polish, cost and time ranges per
item and in total from the user's own history, the share of finished runs of that size that reached the end without a human, and
the gates. "Użyj w kreatorze" fills the wizard fields; nothing is frozen or started until START. Items only a human can do are
prefixed `[człowiek]` and become `human_required` roadmap items: AAW does not do them and leaves them on the Human Gate list; they
are not counted as iterations or cost. The same roadmap forecast also appears in the wizard summary for hand-written roadmaps.
The intake call's own cost is shown and an audit copy is kept in `<AAW home>/intake/`.

Forecast honesty: item ranges use the user's all-in per-iteration cost of the same kind (≥3), else of all kinds, else "brak danych";
totals add quartiles on purpose (wider than the true spread) and the UI says so; a roadmap longer than the iteration fuse is flagged.

## 7. Next (designed, not built)
| # | Item | Why |
|---|---|---|
| ~~E4~~ | ~~recommended model button~~ | **done, §7a** |
| ~~E5~~ | ~~opt-in controlled exploration~~ | **done, §7a** |
| ~~E6~~ | ~~idea intake with horizon and cost before START~~ | **done, §7a** — next step: feed the intake's risks into the charter's `risk_guidance` |
| E7 | **Human decision as ground truth**: gate Accept/Reject (and later "tests green after integration") become a quality label next to `solved` | removes the reviewer-is-also-a-model blind spot |
| E8 | Planner-declared `task_kind` + a "this was something else" correction | better kinds, learned per user |
| E9 | Server-sent events + a desktop notification at the gate; per-step token streaming when the CLIs expose it | true real time instead of 1.5 s polling |
| E10 | Reviewer benchmark from seeded-defect diffs (recall of HIGH defects, false-blocking rate) built from past reviews | picks the light and the serious reviewer the same passive way |
| E11 | Opt-in anonymised seed sharing | skips the cold start for common work types |
