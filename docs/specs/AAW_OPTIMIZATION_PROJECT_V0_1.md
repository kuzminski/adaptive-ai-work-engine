# AAW Optimization Project V0.1 — long chains, one serious review, measured cost

Status: chains 1–3 implemented and tested, chain 4 (experience, below) implemented. **No lab run is needed**: the model
optimum is determined passively from real runs — see `AAW_UX_REALTIME_EXPERIENCE_V0_1.md`. The lab benchmark (`BENCH/`) is an
optional cold-start seed; live validation of chain mode on a real provider has still not been run (it spends quota).

## 1. Philosophy this project encodes

1. **One strong plan up front.** The strongest planner thinks once for a whole chain of work.
2. **Long implementation chains, reviewed lightly.** Many iterations run back to back, protected by deterministic
   self-verification and a cheap, half-hearted review that only a *CRITICAL* defect can stop.
3. **One serious review at the end of the chain**, over the whole diff, on a stronger model. Significant defects
   are repaired and the chain continues.
4. **Small errors are accepted while they do not harm real behaviour.** They are recorded, never lost, and
   **polished once at the very end**.
5. **Measure everything**, and let measured cost per solved task — not taste — choose the implementation models.
6. **It must work and be effective**: every change is behind a switch, the classic cycle is unchanged when the
   switch is off, and nothing here can lower a safety boundary (Git containment, Human Gate, mandate scope).

## 2. Audit — AAW against that philosophy (before this project)

| Question | Finding | Source |
|---|---|---|
| Review rhythm | **Every** iteration ran `PLAN → EXECUTE → SELF_VERIFY → REVIEW → FINAL_REVIEW`. A review model was invoked ~4–6× per iteration (the reviewer's raw-evidence retrieval doubles each review call; pretreatment adds two more). | `autonomy_controller.py`, live evidence |
| Where the money goes | In three live Luna/Sol runs the implementation was **33% of token cost-units and ~4% of list-price dollars; review (prepare + review + final) was 61% of units and ~92% of dollars**. In the Claude E2E run, self-verify + review + final-review were 63% of reported dollars. | `python aaw_telemetry.py evidence EVIDENCE/AAW_AUTONOMY_V0_3_2_*.json` (caveat: the Opus planner calls carry no usage in those reports, so planning is under-counted) |
| Severity policy | A reviewer `REPAIR_REQUIRED` with only soft findings was forced into a repair via `UNSPECIFIED_REPAIR`; every finding with severity HIGH/CRITICAL was blocking. Nothing distinguished "breaks behaviour" from "taste". | `autonomy_contract.normalize_review` |
| Planning | One planner call per iteration (the initial Opus architect only froze a charter and planned iteration 1). | `_do_plan` |
| Polish | None. Non-blocking findings were attached to the iteration and, in practice, forgotten. | — |
| Telemetry | Raw `usage` / `total_cost_usd` sat in per-execution result files; **nothing aggregated them** per run, role, iteration or chain, and no price table existed. | `autonomy_adapters.py` |
| Model choice | Static tiers (`GPT6_LUNA_HIGH → VERY_HIGH → MAX`, Sonnet escalation, Opus planner) chosen by judgment; no measured solve rate or cost per solved task anywhere. | `AUTONOMY_ROLES.json`, `MODEL_RECOMMENDATIONS.json` |

Consequence: with list prices the first lever is **review volume, not the implementer's price**. Choosing a cheaper
implementer saves cents; reviewing less often saves most of the bill. The implementer choice matters mainly through
*rework* (repair loops, escalations) — which is exactly what cost per **solved** task captures.

## 3. Design — Chain Mode (`autonomy_chain.py`, `autonomy_controller.py`)

```
chain of L iterations (default 8)
 it 1..L-1  PLAN* → EXECUTE → SELF_VERIFY → [light REVIEW] ─pass→ PROVISIONAL → ROADMAP_CHECK → next
                                              └ CRITICAL ┘ → REPAIR → back through SELF_VERIFY
 it L       PLAN* → EXECUTE → SELF_VERIFY → REVIEW → FINAL_REVIEW (tier ≥ close_final_floor, whole diff)
                                              └ HIGH/CRITICAL → REPAIR → …  ─pass→ whole chain ACCEPTED
 roadmap ends mid-chain  → the open chain is closed by the same serious review before the Human Gate
 roadmap done            → one controller-authored POLISH iteration over the deferred findings → Human Gate
 * PLAN is skipped inside a chain: the strong planner's `chain_plan` stubs are expanded and re-validated
```

* **Config** — the `chain` block of `AUTONOMY_ROLES.json` (frozen into the run at start, like `routing`):
  `enabled`, `length`, `mid_chain_review` (`PRIMARY`|`NONE`), `mid_repair_min_severity` (`CRITICAL`),
  `close_repair_min_severity` (`HIGH`), `close_final_floor` (`HARD`), `plan_batching`, `polish`.
  `enabled: false` restores the classic per-iteration cycle byte for byte.
* **Severity policy** (`relax_review`, applied before `normalize_review`): findings below the repair threshold are
  removed from the verdict and appended to `state["deferred_findings"]` (deduplicated, with origin and chain). A
  `REPAIR_REQUIRED` whose findings were all deferred becomes `PASS`. Inside a chain a reviewer's own `blocking:true`
  never stops work; at the serious review it does for MEDIUM and above (never for LOW). Failing *checks* still force
  a repair at every stage — a red test is real harm, not taste.
* **Reviewer instructions** change with the mode (`CHAIN_INSTRUCTIONS`): the light reviewer is told to look only
  for behaviour-breaking defects and to prefer PASS; the serious reviewer sees every iteration of the chain, the
  open backlog, and must re-verify deferred HIGH items.
* **Chain plan**: the planner may return `chain_plan` (≤ L−1 self-contained stubs). Each stub is expanded into a
  normal plan and **re-checked by `check_plan`** when its turn comes; a stub invalidated by roadmap movement is
  dropped and the planner is called — never an escalation. Stubs never reach the implementer's PLAN.
* **Final-review floor**: a chain close never runs below `close_final_floor`; if that profile is not runnable on this
  machine the run keeps the default tier and journals `CHAIN_FLOOR_UNAVAILABLE` instead of stopping.
* **Polish**: only when the roadmap is *done* (not when a fuse stopped it), once per run, ≤ `max_findings`, highest
  severity first, inside the mandate's allowed/forbidden areas (out-of-scope findings are reported, not polished);
  it gets the full serious review. What it attempted leaves the open backlog; the Human Gate lists what remains.
* **Safety unchanged**: every phase boundary still runs `assert_safe`; a chain close that escalates leaves the
  unreviewed iterations `PROVISIONAL` (never silently accepted, shown as "PASS wstępnie — bez review serii");
  promotion remains human-only.

### What is deliberately *not* promised
A provisional iteration can be wrong in a way only the chain close finds; the repair then lands on the chain's last
iteration, and may touch earlier iterations' files (repairs are scoped to the findings, not to the iteration). That is
the price of the philosophy and the reason the close review is stronger than the old per-iteration review.

## 4. Telemetry V1 (`aaw_telemetry.py`)

One `AAW_TELEMETRY_V1` record per executor call, appended by the controller to
`<STATS_ROOT>/<run>/AUTONOMY/telemetry.jsonl`: execution/iteration/chain ids, role, executor, **category**
(PLANNING / IMPLEMENTATION / VERIFICATION / REVIEW), profile, model, effort, tier, review mode, outcome, controller
wall time, normalized tokens (`input_total`, `input_cached`, `output`, `reasoning`) and cost.

* **Cost is never invented**: `REPORTED` (Claude CLI `total_cost_usd`), `ESTIMATED` (tokens × `MODEL_PRICING.json`) or
  `UNPRICED`. `proxy_units` (uncached input 1, cached 0.1, output 5) is a price-independent proxy.
* `MODEL_PRICING.json` rows are **list-price equivalents read from secondary sources and marked NOT_VERIFIED**:
  AAW normally runs on subscription quota, so dollars are a yardstick. Verify before deciding anything expensive.
* Writer never raises (a broken disk cannot stop a run); old runs are rebuilt from state + RESULTS artifacts.
* Reports: `python aaw_telemetry.py run RUN_ID`, `python aaw_telemetry.py evidence EVIDENCE/*.json`
  → totals, share by category, by executor / profile / review mode / chain, cache hit ratio, cost per accepted iteration.

## 5. Which benchmark, and why (relevance)

Candidates considered: public SWE-bench Pro / Terminal-Bench style leaderboards (they publish cost per solved task
and are the right *prior* for which profiles to test), and an in-house suite. Public numbers cannot decide AAW's
implementer because (a) the harness changes spend by about 2× at equal accuracy, (b) they do not cover
`codex exec` / `claude --print` at AAW's effort levels, (c) AAW's unit of work is a *bounded iteration inside a
frozen mandate with forbidden paths*, not an open issue. **AAW-Bench** (`BENCH/`) therefore measures exactly that:

* 9 tasks, three bands that map onto AAW's own complexity tiers (EASY→NORMAL→`implementer_default`,
  MEDIUM→HARDER→`implementer_harder`, HARD→SIGNIFICANTLY_DIFFICULT→`implementer_hard`): bug fix, feature from spec,
  multi-file change with exact arithmetic, spec-implementation with edge cases, behaviour-preserving refactor under a
  forbidden-path constraint, cross-module bug, a stateful algorithm and a matching problem with tie-break rules.
* Each task runs through the **production `AutonomyController` and the production `execute` executor**; only planner,
  self-verifier and reviewers are scripted, so every token, second and dollar is the implementation's own.
* Scoring is objective: hidden acceptance tests + forbidden-path check of the produced diff. `validate` proves with
  no model that every task fails on its start repo and passes with its reference solution (it already caught two bad
  expectations while the suite was written).
* Production feed: `python BENCH/aaw_bench.py production RUN_ID` turns a real run's telemetry into the same rows
  (accepted iteration = solved, implementation cost only), so the model choice keeps improving from real work.

## 6. How the optimum is determined

For each profile: solve rate (Wilson 95% interval), **cost per solved task** = total spend ÷ solved, mean wall time.
Then, because AAW already escalates (`implementer_default → harder → hard → capability escalation`):

* **Pareto front** on (cost per solved, solve rate).
* **Escalation ladders** (up to 3 rungs): expected cost per task of "try A, on failure B, then C" computed per task from
  measured solve rates and costs (attempts independent), success = 1 − ∏(1−pᵢ). The recommended ladder is the cheapest
  one whose success clears `--ladder-floor` (default 0.9).
* **Per-band recommendation** for the three implementation tiers: cheapest profile whose solve rate in that band clears
  `--floor` (default 0.8) with at least `--min-n` trials; otherwise `INSUFFICIENT_DATA` / `NO_PROFILE_MEETS_FLOOR`.
  Nothing is recommended on thin data, and with the proxy basis the report warns that cross-family comparisons are
  not price-aware.

**Current optimum: not determined here — and the product says so rather than guessing.** The repository holds live token
data for Luna runs but no solve/fail outcomes per profile, which is what the decision needs. That data now accumulates by
itself: every real iteration adds a row to the per-kind benchmark (`aaw_experience.py`, "Doświadczenie" page), and the same
analysis (`aaw_benchstats.py`) powers both this lab suite and the product, so a lab run is an optional seed, not a workflow.

## 7. Project plan (executed as chains, per the philosophy)

| Chain | Content | Status |
|---|---|---|
| 1 Telemetry | schema, normalizer, pricing table, controller emission, report CLI, legacy-run reconstruction | done, tested |
| 2 Chain Mode | config, severity policy, light/serious review, provisional acceptance, chain plan, floor, polish, product view labels | done, tested (34 tests) |
| 3 Benchmark | 9 verified tasks, production-path runner, scorer, analyzer (Pareto, ladders, bands), production feed | done, tested; **live run pending** |
| ▶ Serious review of 1–3 | self-review of the whole diff + full regression | done (findings fixed: see CHANGELOG) |
| 4 Experience | passive per-kind benchmark, live run panel, forecast, settlement, horizon (`AAW_UX_REALTIME_EXPERIENCE_V0_1.md`) | done, tested, checked in the browser |
| 5 Live validation | one real chain run with the product defaults; the Experience page then shows its cost split by itself | **next — needs go-ahead (quota)** |
| 6 Optional lab seed | `BENCH/` staged run only if the user wants a cold start | optional |
| 7 Closing the loop | one-click recommended model, opt-in exploration, idea-intake stage, human decision as label (see UX doc §7) | next |

## 8. Optional: the lab seed (not the main path)

Normal use needs none of this — the product's Experience page fills from real runs. If a cold start is wanted: the lab run
consumes provider quota (NO_EXTRA_PAID_USAGE) and wall time (a trial is minutes, not seconds).

```powershell
python BENCH\aaw_bench.py validate                                   # free, any time
python BENCH\aaw_bench.py run --profiles GPT6_LUNA_HIGH,GPT6_LUNA_VERY_HIGH,CLAUDE_SONNET_5_5_MEDIUM,SOL_HIGH --repeats 1   # dry run: prints the plan
python BENCH\aaw_bench.py run --profiles <same> --repeats 1 --yes-spend     # stage 1: 36 trials, resumable
python BENCH\aaw_bench.py analyze BENCH\results\aaw_bench_results.jsonl
```

Typical implementation call in the evidence: 0.2–0.5M input tokens (85–90% cached) and 2–8k output tokens.
Results append to `BENCH/results/aaw_bench_results.jsonl` and are skipped on re-run, so an interrupted benchmark resumes.
