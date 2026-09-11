# AAW — OPERATOR RECOVERY & EDIT SAFETY V0.2

Status: **PASS — recovery/edit-safety hardening complete**
Scope stop: no planner, merge, rejoin, routing redesign, or path-scoped carry-forward.

## Baseline authenticated

The accepted V0.1 report records 193 passing tests. The literal implementation
available at the start of this continuation had already advanced to **213 passing
root tests**. V0.2 finishes at **221 passing root tests**. Recursive discovery is
not the authoritative command because historical `WORKFLOW_SMOKE_TEST/worktree*`
fixtures contain six import-incompatible nested checkouts.

## Design decisions

### Recovery is proof-gated, never a generic rollback

Each active run owns its selected worktree through both an in-process lease and a
worktree-specific cross-process lock in Git metadata. On settlement, the bridge
immediately writes `WORKFLOW/recovery_ownership.json` with:

- originating run id and exact worktree;
- runner-proven clean baseline HEAD/status;
- modified and untracked paths;
- status, existence, size, and SHA-256 fingerprint per path;
- the frozen post-run snapshot and operator resolution.

Inspection re-reads HEAD, status, and fingerprints. Any drift, missing baseline,
rename, conflict, directory-only status, malformed evidence, or changed path
sets `AMBIGUOUS_OWNERSHIP` and disables cleanup. Safe cleanup uses only:

- `git restore --source=<baseline> --staged --worktree -- <explicit tracked paths>`;
- individual `unlink` calls for exact, fingerprint-matched untracked files;
- removal of now-empty parent directories only.

There is no `git reset --hard`, recursive delete, merge, commit, push, or history
rewrite. `Keep changes` changes only the recovery resolution; worktree bytes stay
untouched. `Discard changes produced by this run` requires an operator confirm.

### BUILD history is draft-only

`DraftHistory` is an in-memory, 100-entry history of exactly two authoring
surfaces: the workflow draft and its separate node layout. It cannot contain a
run id, runtime overlay, routing journal, execution ledger, artifacts, or Git
state. It covers node create/delete/move/property changes, edge
create/delete/edit, start-node changes, and insert-into-edge as one compound
gesture. `Save` resets the clean baseline. `Discard unsaved` restores the last
saved workflow and layout. Shortcuts: Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z, Ctrl/Cmd+Y.

### Contextual repair does not invent routing

The existing validator remains authoritative. A conflicting unconditional
`FIRST_MATCH` refusal is now attributed to every affected edge. On an affected
edge the canvas offers one action: keep that selected edge unconditional and
delete the other unconditional fallback(s). It creates no predicate and makes
no routing choice beyond the edge the operator explicitly selected. Node and
edge diagnostics stay attached to their graph elements and inspectors.

`depends_on` is labelled and explained as required prior completions, not a wire
or route. The inspector names the selected prerequisite ids and the no-prerequisite
case explicitly.

## Implementation

- `run_recovery.py`: ownership snapshots, bounded diff preview, cross-process
  worktree lease, fail-closed inspection, explicit-path discard.
- `aaw_bridge.py`: recovery manifest lifecycle, inspect/keep/discard actions,
  active-worktree ownership, multi-edge validation attribution.
- `aaw_bridge_server.py`: `/api/run/worktree/keep` and
  `/api/run/worktree/discard`; cancellation waits briefly for settlement so the
  ownership snapshot is available to the operator.
- `UI_PROTOTYPE/draft_history.js`: bounded undo/redo state machine.
- `UI_PROTOTYPE/aaw-canvas-live.html`: BUILD controls, shortcuts, direct
  diagnostics, one-click FIRST_MATCH repair, worktree diff/ownership panel,
  keep/discard resolution, and correct live `Stop` affordance.
- `aaw_llm_test_adapter.py`: opt-in, worktree-contained `write_files` support
  used only to make partial work observable in the real browser trial.

Workflow JSON, layout JSON, routing, multirouting, repair lineage, runtime event
authority, and execution-ledger schemas are unchanged.

## Tests and evidence

Final authoritative command:

```powershell
$tests = Get-ChildItem -LiteralPath . -File -Filter 'test_*.py' |
  Sort-Object Name | ForEach-Object FullName
python -m pytest -q @tests
```

Result: **221 passed in 243.90 s**.

New executable evidence includes:

- run-owned tracked and untracked attribution followed by exact cleanup;
- hash drift after cancellation causing `AMBIGUOUS_OWNERSHIP` and a destructive
  refusal while retaining the changed file;
- non-destructive keep resolution;
- same-worktree overlap refused across separate bridge instances;
- a second run passing the clean-worktree guard after discard;
- absence of hard reset and recursive removal in the cleanup implementation;
- every conflicting unconditional edge receiving an edge diagnostic;
- Node-executed undo/redo traversal over every required BUILD mutation, with
  runtime state unchanged and Save resetting history;
- canvas/transport endpoint agreement and Python/JavaScript syntax checks.

## Operator trial — real browser, no terminal interaction

Playwright drove the served canvas at 1600×900 against a real temporary Git repo
and registered worktree. The scripted adapter was only the existing provider
seam; runner, cancellation, journal, workspace guard, ownership, HTTP/SSE, and
Git cleanup were real.

1. In BUILD, edit the N01 brief; Undo restored the saved definition and enabled
   Redo without touching runtime state.
2. Start run `AAW_20260911_014402_a94c79a8` from the canvas.
3. Stop during N01; the journal recorded `RUN_CANCELLED` at sequence 2.
4. The banner reported `CANCELLED · PARTIAL_WORK_PRESENT`.
5. Inspect showed originating run id, `RUN_OWNED`, mixed change `NO`, untracked
   `operator_partial.py`, status `??`, and its textual diff.
6. `Discard changes produced by this run` plus confirmation returned the
   worktree to the proven clean baseline and displayed `ready to rerun`.
7. Run again from the same canvas: `AAW_20260911_014526_e5ae5138` reached
   `RUNNING · N01 · seq 1`; it was then cancelled and cleaned through the same
   UI so the temporary trial worktree finished clean.

The only browser console error was the browser's optional `/favicon.ico` 404;
there were no application JavaScript errors.

## Remaining friction, ranked by observed impact

1. **High — Keep is intentionally not a clean-worktree resolution.** It transfers
   responsibility to the operator but leaves the worktree dirty, so the strict
   runner still refuses another run until those changes are committed or moved.
   Automating commit/stash would be a new Git workflow and was not inferred here.
2. **Medium — pre-V0.2 or crashed-run evidence fails closed.** Without a sealed
   ownership manifest, cleanup is unavailable. A stale cross-process lease also
   requires explicit administrative recovery rather than silent takeover.
3. **Medium — any post-settlement edit disables cleanup.** Even a harmless manual
   change to one owned file breaks its fingerprint. This is deliberate safety,
   but the operator must then resolve it outside the one-click path.
4. **Low — previews are bounded.** Text/binary diff preview is capped at 200 KB;
   large or binary untracked files show bounded evidence rather than full content.
5. **Low — text editing history is input-event granular.** Long typing sessions
   can consume several of the 100 history entries; semantic operations and
   insert-into-edge remain atomic.

`path-scoped carry_forward` remains explicitly deferred and remains a prerequisite
for any future merge/rejoin work.
