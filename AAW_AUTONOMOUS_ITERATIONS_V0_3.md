# AAW Autonomous Iterations V0.3

V0.3 adds the `DEFAULT_AUTONOMOUS` model policy to the V0.2 controller. The
policy selects configured profile IDs; profile definitions and `MODEL_CATALOG`
own provider runtime IDs and effort mappings. The controller does not substitute
an unavailable profile.

## Default policy

| Stage | Profile ID | Runtime mapping | Selection rule |
| --- | --- | --- | --- |
| Initial architect | `OPUS_5_5_HIGH` | Exact Claude 5.5 runtime mapping unavailable | One initial directional charter; fails closed if unavailable |
| Normal implementation | `GPT6_LUNA_HIGH` | `gpt-6-luna` / `high` | Default bounded implementation |
| Harder implementation | `GPT6_LUNA_VERY_HIGH` | `gpt-6-luna` / `xhigh` | Harder plan or frozen charter risk floor |
| Significantly difficult implementation | `GPT6_LUNA_MAX` | `gpt-6-luna` / `max` | Highest Luna tier for bounded work |
| Exceptional implementation | `SONNET_5_5_MEDIUM` | Exact Claude 5.5 runtime mapping unavailable | Only after a failed Luna Max capability attempt with evidence, or a human mandate override |
| Self verification | `GPT6_LUNA_HIGH` | `gpt-6-luna` / `high` | Deterministic checks by default; model call only for justified semantic uncertainty |
| Review pretreatment | `GPT6_LUNA_VERY_HIGH` | `gpt-6-luna` / `xhigh` | Organizes evidence; never decides correctness or returns a verdict |
| Primary reviewer | `SOL_6_1_LIGHT` | `gpt-6.1-sol` / `low` | Fresh-context review of the packet and evidence manifest |
| Default repair | `GPT6_LUNA_VERY_HIGH` | `gpt-6-luna` / `xhigh` | Repairs selected findings only |
| Hard repair | `GPT6_LUNA_MAX` | `gpt-6-luna` / `max` | Multiple blocking findings, later repair attempt, or critical finding |
| Default final review | `SOL_5_6_LIGHT` | `gpt-5.6-sol` / `low` | Separate fresh-context final review |
| Hard final review | `SONNET_5_5_MEDIUM` | Exact Claude 5.5 runtime mapping unavailable | Architecture or high-impact evidence, repeated repair, uncertainty, or a frozen charter floor |
| Critical final review | `OPUS_5_5_MEDIUM` | Exact Claude 5.5 runtime mapping unavailable | Critical contract/safety evidence or a frozen charter floor |

The initial architect freezes the human objective, roadmap items and
dependencies, acceptance criteria, boundaries, mandatory Human Gate conditions,
and any evidence-backed risk guidance. Each later planner receives that
charter/hash and current roadmap state. A plan that changes the charter or
selects unknown, human-required, dependency-blocked, or out-of-mandate work
escalates before implementation.

Risk guidance can set a minimum Luna implementation tier and a minimum final
review tier for named roadmap items. Runtime selection records the policy ID,
reason, evidence, previous attempt, escalation source, and execution ID in
`MODEL_POLICY_SELECTED`; the V0.4A descriptor and V0.4B ledger remain the
invocation and lifecycle authorities.

## Review and repair

The deterministic review packet is built from the frozen task, changes, checks,
warnings, deviations, uncertainties, and findings. Pretreatment can add a
source-backed index only. The controller rejects verdict fields and retains the
original evidence if pretreatment is invalid or incomplete.

Review prompts receive the compact packet, directional charter, hashes, source
manifest, and relevant references. Raw files are not sent by default. A reviewer
may request up to five named sources with reasons. The controller verifies the
source path, hash, and size, caps a retrieval round at 1 MB total, records the
request and delivery, and permits one retrieval round. A second request or an
invalid reference escalates.

Every repair returns through self verification, review pretreatment, and a
fresh primary review before final review. Existing repair-count and no-progress
limits still stop repeated identical findings.

## Iteration and Human Gate

After a final review passes, its selected tier is used to plan the next bounded
iteration: Sol 5.6 Light, Sonnet 5.5 Medium, or Opus 5.5 Medium. The initial
Opus 5.5 High architect is not rerun. Planning ends when no valid autonomous
roadmap item remains, including when remaining work is human-required or
blocked by a human-required/skipped dependency. The run then creates its final
candidate and enters `AWAITING_HUMAN`. Promotion remains a separate human-only
action.

## Availability

GPT-6 Luna and GPT-6.1 Sol profiles are configured through the Codex CLI with
dynamic runtime preflight. The three exact Claude 5.5 profiles are recorded as
`KNOWN_BUT_UNAVAILABLE`; no older Claude model ID is treated as equivalent.
When a selected profile is not runnable, the run stops with
`ROLE_PROFILE_UNAVAILABLE` before dispatch.

## Validation

`test_autonomy_policy_v0_3.py` contains 31 deterministic tests covering policy
tier selection, charter freezing, continuation, unavailable-profile behavior,
Human Gate routing, seven pretreatment/raw-evidence safety cases, and strict
provider-schema requirements. Existing V0.1/V0.2 lifecycle, ledger, lock,
Git-containment, and Human Gate tests remain in place.

A bounded mixed live run reached the GPT-6 Luna implementation and review
pretreatment, then the GPT-6.1 Sol primary reviewer rejected the response schema
before review. The report preserves the provider error and execution/session
IDs. The schema was corrected afterward and covered by the strict-schema
regression test; that captured live run was not resumed or promoted. The
initial Claude Opus 5.5 architect was scripted only in the mixed fixture because
the exact runtime mapping is unavailable. Final-review invocation and live
roadmap exhaustion therefore remain unverified. Earlier bounded timeout
attempts are preserved as separate evidence files. Availability claims are
limited to the observed environment and run.
