# AAW V0.4C — Analytics V2 contract

Schema marker: `AAW_ANALYTICS_INDEX_V2`. Identity contract: unchanged
`AAW_EXECUTION_DESCRIPTOR_V0.4A`. Ledger contract: unchanged
`AAW_EXECUTION_LEDGER_V0.4B`.

Module: `CONTROL_CENTER/ANALYTICS/aaw_analytics.py`.
Published index: `CONTROL_CENTER/ANALYTICS/aaw_analytics.sqlite`.

## 1. Raw vs derived authority

`03_STATS` is the **raw evidence authority** and is never written, repaired or
backfilled by analytics. The SQLite index is **derived, disposable and
rebuildable**: no runner reads it, and deleting it loses nothing that cannot be
reproduced from raw evidence.

Every derived row is traceable to the source file it came from
(`source_file` + `entity_source`), so any number in Insights can be walked back
to the artifact that produced it.

## 2. Execution grain

The analytical grain is **`execution_id`** — one `EXE_<32 hex>` identity per
executable invocation. V1/V1.1 joined executions to nodes through
`run_id + node_id`, which can collapse or multiply repeated invocations of one
logical node. V2 removes that join.

`node_id` remains **logical workflow identity**. `provider_session_id` remains
**observed execution metadata and never identity**.

```
run → logical node → execution → result / validation / review / commit
                                  / preprocess / candidate / human decision
```

Repeated invocations of one node stay separate executions:

```
S1
├── EXE_a   (first invocation)
├── EXE_b   (retry_of_execution_id = EXE_a)
└── EXE_c   (REPAIR: originating_review_execution_id = EXE_r)
```

One logical node row, three execution rows. A REPAIR is **not** a retry and
never receives `retry_of_execution_id`.

## 3. Explicit joins only

Relationships are established **only** through explicit evidence identities:

| Relationship | Key |
|---|---|
| invocation | `execution_id` |
| review finding | `(review_execution_id, finding_id)` |
| repair selection | finding key + `repair_execution_id` |
| delta review | `repair_execution_id` + `original_review_execution_id` + finding key |
| git commit | `(repository, commit_hash)` |
| preprocess → downstream | `preprocess_id` + `downstream_execution_id` |
| human decision | `human_decision_id` → exact `candidate_id` |

Never joined by: timestamp proximity, filename similarity, a repeated
`node_id`, a shared `provider_session_id`, or chronological order.

`F001` alone is not a finding identity. `(EXE_review_a, F001)` and
`(EXE_review_b, F001)` are two different findings.

## 4. Modern / legacy boundary

Every indexed row is classified:

| Class | Meaning |
|---|---|
| `MODERN_EXECUTION_GRAIN` | explicit `execution_id` under the V0.4A contract |
| `LEGACY_SOURCE_GRAIN` | pre-V0.4A evidence; `execution_id` stays NULL |
| `FIXTURE` | display class when a fixture rule fired (see §7) |
| `UNKNOWN` | grain cannot be determined from the evidence |

**No `EXE_*` is ever synthesised for historical evidence.** A legacy row instead
carries a `row_locator` derived from `source rel_path + source sha256 + JSON
pointer`. It is a *source record locator*, never an execution identity, and is
never presented as one.

## 5. Entities

`run`, `logical_node`, `execution`, `execution_lifecycle`, `validation`,
`review_finding`, `review_producer_link`, `finding_link`, `preprocess`,
`commit_record`, `commit_producer_link`, `candidate`,
`candidate_execution_link`, `human_decision`, `source_file`, `entity_source`,
`ingest_diagnostic`, `entity_conflict`, `ingest_unit`.

Only `source_file` is a foreign key target. Entity-to-entity relations are
deliberately **not** foreign keys: analytics must be able to record a lifecycle
observation whose descriptor is unreadable, a finding whose repair never
happened, or a commit with no attributable producer. Those become explicit
*unlinked* counts instead of silent insertion failures.

## 6. Source precedence

Precedence is defined **per field and per entity**, never by file mtime and
never "newest file wins".

| Fact | Authority | Fallback |
|---|---|---|
| execution identity + provenance (node/subtask/kind/provider/harness/model/effort/profile/created_at/retry/contract hashes) | `EXECUTIONS/<EXE>.json` descriptor | state `executions[]`, then legacy node telemetry |
| usage, wall time, provider session | state `telemetry[]` joined by `execution_id` | — |
| semantic node verdict | node result / `subtask_results` / `review` / `delta_review` / `machine_gates` | workflow `completed_nodes[]` outranks telemetry `outcome` |
| execution lifecycle (intent/start/close/PID/effect certainty) | `LEDGER/execution_events.jsonl` | none — absence is UNKNOWN |
| git commit | commit artifact / state `commit_records` **cross-validated with** `COMMIT_RECORDED` | — |
| candidate | `candidate.json` artifact | state `candidate` |
| human decision | immutable `HDE_*.json` artifact | state `human_decisions[]`, then `HUMAN_DECISION_RECORDED` |

No source replaces another:

- `EXECUTION_CLOSED` does not replace a reviewer result. The ledger's observed
  close outcome is stored separately as `execution_lifecycle.ledger_outcome`.
- `COMMIT_RECORDED` does not mean a candidate was accepted.
- `HUMAN_DECISION_RECORDED` references the decision artifact; it does not
  replace it.
- A REPAIR execution has **no** recorded semantic outcome in current evidence,
  and is not given the ledger's close outcome as a substitute.

## 7. Fixture classification

`fixture_class` ∈ `FIXTURE_SELF_TEST` · `FIXTURE_E2E` · `FIXTURE_SMOKE` ·
`NOT_FIXTURE` · `UNKNOWN`, always paired with `fixture_evidence` recording
**which rule fired**, so every classification is auditable.

Rules, first match wins, all on producer-declared fields:

1. `DESCRIPTOR_FIXTURE_CLASS` — `fixture_class` recorded in the V0.4A descriptor.
2. `DECLARED_SELF_TEST_JOB_ID` / `DECLARED_E2E_JOB_ID` / `DECLARED_SMOKE_JOB_ID`
   — a declared `job_id` in the known fixture set.
3. `DECLARED_TEMP_REPOSITORY` — the declared repository/worktree is inside the
   OS temporary directory. Real product work is never done in a throwaway temp
   repository.
4. `DECLARED_SMOKE_TEST_REPOSITORY:<marker>` — the declared repository is one of
   AAW's own `*_SMOKE_TEST` / `LOCAL_LLM_SMOKES` fixture repositories.

Nothing else is classified. Absent evidence stays `UNKNOWN` and is **counted and
displayed** rather than guessed at. `UNKNOWN` is *not* treated as a fixture.

**Default view split:**

- model-performance views (`outcome_by_model_profile`, `review_first_pass`,
  `median_exec_time`, and the median card) **exclude** fixtures by default;
- operational views (`usage_over_time`, `lifecycle_health`, call counters,
  preprocess decisions) **include** them, because excluding a real invocation
  would understate consumption;
- `data_quality` always counts everything and reports the fixture share.

`include_fixtures` overrides the default in both directions everywhere.

## 8. Lifecycle interpretation

Derived only from observed V0.4B events, read through
`execution_ledger` (its reader and validator are reused, never reimplemented):

| State | Observed |
|---|---|
| `INTENT_ONLY` | intent, no start observation |
| `STARTED_OPEN` | start observed, no terminal event |
| `CLOSED` | terminal event observed |
| `LIFECYCLE_CONFLICT` | the V0.4B validator reports a lifecycle error for that identity |
| `UNKNOWN` | no ledger evidence at all (`basis = NO_LEDGER_EVIDENCE`) |

- `STARTED` is never inferred from `INTENT`.
- `CLOSED` is never inferred from workflow status.
- **A missing `CLOSED` is unresolved lifecycle, not an analytical FAIL.**
- `intent_sequence` / `started_sequence` / `closed_sequence` express **ledger
  append order only** and are never used as time.
- The ledger is never repaired during ingestion; damaged tails and damaged
  records become diagnostics.
- A ledger observation whose execution is not indexed becomes
  `LIFECYCLE_WITHOUT_INDEXED_EXECUTION`; no execution row is invented from
  ledger payloads, because the ledger is not identity authority.

## 9. Missingness semantics

| Situation | Recorded as |
|---|---|
| absent usage | `NULL` + `usage_observed = 0` — **never zero-filled** |
| absent model/effort | `NULL` |
| absent semantic outcome | `NULL` + `outcome_source = NULL` |
| absent lifecycle | `UNKNOWN` |
| absent human decision | no row; coverage reported as `n/eligible` |
| absent quality assessment | `NULL` = UNKNOWN, **never a model verdict** |
| skipped preprocess | `preprocess_id` present, `execution_id = NULL`, `decision_status = SKIPPED` — not an LLM invocation |
| standalone preprocess with no downstream identity | `downstream_link_confidence = UNLINKED` |
| legacy flat run status | `UNKNOWN` + `final_status_basis = NO_EXPLICIT_RUN_STATUS` |

`ACCEPT` is a release verdict and is never converted into a model-quality
`PASS`. `LEFT_FOR_LATER` is never a rejection. Release-verdict coverage and
quality-assessment coverage are reported as **two separate numbers**.

## 10. Conflicts

When two authoritative sources disagree, neither is silently chosen. The
disagreement is written to `entity_conflict`, a `DATA_INTEGRITY_ERROR`
diagnostic is raised where the identity itself is broken, and the affected row
is flagged `conflict = 1`.

A conflicted entity stays partially queryable, but **relationship metrics that
depend on the conflicting field exclude it** — e.g.
`outcome_by_model_profile` filters `conflict = 0`.

Handled cases:

- same `execution_id`, different run/node/kind/model → original preserved,
  incoming row **quarantined**, never last-write-wins;
- same `candidate_id`, different content identity hash;
- same finding key claimed by two runs;
- same `human_decision_id` claimed by two runs;
- same commit identity with incompatible attribution or producers;
- descriptor vs state provenance disagreement;
- workflow node verdict vs telemetry outcome disagreement.

## 11. Data quality

Insights always shows, over **all** evidence: modern/legacy/unknown runs; runs
with a ledger; modern executions vs legacy source-grain rows; LLM vs machine-gate
executions; the five lifecycle states; model-provenance, token-telemetry,
semantic-outcome and review-lineage coverage; unlinked preprocess / findings /
repairs / commits / lifecycle rows; commit cross-validation; candidates and
Human Gate count; release-verdict and quality-assessment coverage separately;
the fixture share including how many runs are fixture-`UNKNOWN`; and the
integrity block (integrity errors, entity conflicts, conflicted executions,
parse errors, unrecognised sources, ledger diagnostics).

`analytics_readiness` returns the same picture programmatically. It is
explicitly **`EVIDENCE_READINESS`** and never emits an adaptive-readiness
verdict — whether AAW may act on this evidence is a governance decision that
lives outside analytics.

## 12. No causal or ranking claims

Analytics V2 is **descriptive**. Model outcomes are grouped inside one node
type / role, always with `N`, and labelled `OBSERVATIONAL_ONLY`. Allocation was
not randomised, so no comparison here supports a model ranking, a "best model",
a difficulty score, or automatic selection. Only deterministic difficulty
dimensions already present in descriptors are ingested
(`input_contract_hash`, `selection_reason`, `policy_version`, `fixture_class`);
absent fields stay `NULL` and nothing is scored.

## 13. Rebuild, refresh, rollback

- The ingestion unit is a **run directory** (flat legacy files form one unit),
  keyed by a content signature of `rel_path + bytes + sha256` per file — so an
  appended ledger or an in-place edit is detected, and an unchanged SHA is not
  assumed to mean nothing changed anywhere.
- `--refresh` re-ingests only changed units, deleting a unit's rows before
  rebuilding them. Repeated refresh is **idempotent**: no duplicate events,
  executions or lifecycle rows.
- `--rebuild` builds into `aaw_analytics.sqlite.v2build.tmp`, runs
  `validate_index`, and only then **atomically replaces** the published index. A
  failed build or a failed publish leaves the working index untouched.
- Two complete rebuilds of unchanged evidence produce **equivalent analytical
  content**. Compared with `normalized_snapshot()`, which excludes surrogate
  ids, ingest timestamps and mtimes and resolves `source_id` to a relative path;
  the SQLite files themselves need not be byte-identical.
- **Rollback:** delete the index and rebuild from raw evidence. No raw-data
  migration exists or is needed. The V1.1 module and index are kept beside the
  V2 module as `aaw_analytics_V1_1_ROLLBACK.py.bak` /
  `aaw_analytics_V1_1_ROLLBACK.sqlite.bak`.
- A V1/V1.1 index found at the published path is not appended to: it is replaced
  by a full validated V2 build.

## 14. Corrected V1 metrics

Numbers below compare V1.1 and V2 over the **same 711-file `03_STATS` snapshot**
(the state before the V0.4C acceptance self-tests appended two further fixture
runs), so the difference is the join, not the evidence.

V1's `execution ↔ node` join on `(run_id, node_id)` silently **dropped 92 of the
275 execution rows** from every metric built on it. 49 of those were machine
gates (which do not belong in a model metric anyway), but **43 were
model-bearing LLM-family executions — 19 REPAIR, 19 DELTA_REVIEW, 5
CLASSIFIER — that disappeared entirely**. The join was also structurally able to
multiply or collapse repeated invocations of one logical node; that failure mode
had not yet been realised in this dataset, because no run so far invokes the
same node twice.

Corrections carried into V2:

| Metric | V1 | V2 | Cause |
|---|---|---|---|
| executions in outcome-by-model | 162 | 224 | 162 + 43 rows the node join dropped + 21 legacy rows V1 suppressed via `link_confidence='HIGH'` − 2 rows V2 quarantines for a source conflict |
| human acceptance coverage | `0/53` | `12/25` | V1 read only the state `human_verdict` field, which is always null; the decisions live in `HDE_*` artifacts |
| median execution time | 67.2 s (N=14, node rows) | scope-labelled, fixture-excluded by default | V1 measured node wall time and mixed fixture stubs into the same sample |
| review first pass | `n=53` run-level | `n=25` explicit lineage + `n=28` legacy source grain (33 = 14 + 19 first-pass, 20 = 11 + 9 repair-required) | V1 mixed both grains into one run-level number |
| legacy flat run status | last file read wins | `UNKNOWN` | there is no explicit run-level status in that evidence |
| unrecognised sources | 146 `UNKNOWN_LAYOUT` | 0 | descriptors, candidates and decisions are now recognised layouts |
| preprocess downstream links | node-id only | 97 by explicit execution id, 52 by node id, 2 unlinked | `downstream_execution_id` is now used |

Legacy-only metrics keep their source-grain meaning and are reported apart from
modern ones rather than blended.

## 15. Boundary

V0.4C **adds** a derived index. It does not implement recommendation policy,
adaptive routing, model ranking, difficulty scoring, or an Execution Supervisor;
it does not alter ORCA, add workflow nodes, change V0.4A identity semantics or
V0.4B event taxonomy, backfill historical execution identity, or mutate
`03_STATS`.
