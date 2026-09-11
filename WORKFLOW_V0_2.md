# AAW Static Workflow Runner V0.2

`workflow_runner.py` wykonuje wcześniej zdefiniowany, statyczny workflow. Odczytuje plan JSON, uruchamia kolejne node'y, zapisuje kontrolowane handoff artifacts, rozstrzyga jawne przejścia `PASS`/`FAIL`, obsługuje ograniczoną pętlę repair i zatrzymuje się na Human Gate.

Runner nie jest plannerem, routerem, selektorem modeli, frameworkiem agentowym ani zamiennikiem ORCA. Nie tworzy node'ów, nie zmienia celu i nie podejmuje decyzji poza zamrożonym workflow.

## Manual execution profiles and frozen bindings

`IMPLEMENTER_PROFILES.json` is a deliberately small V0.2 catalog of manual runtime/UI profiles: `TERRA_HIGH`, `SONNET_HIGH`, and `SOL_MEDIUM`. It is not a second model registry, a benchmark ranking, or an automatic selection policy. `IMPLEMENT` and `REPAIR` use `SOL_MEDIUM` when their existing template default is `gpt-5.6-sol / medium`.

An operator can override individual eligible nodes at launch only. The runner resolves each selected/default profile to `harness`, `runtime_model_id`, and `effort`, writes immutable-for-the-run `03_STATS/<AAW_RUN_ID>/WORKFLOW/workflow_bindings.json` before the first LLM node, and uses that frozen mapping for all later calls. GUI changes after start cannot affect it. `REVIEW` remains the current workflow default.

`SONNET_HIGH` stays visible when its runtime is unavailable. Dry run reports `availability: UNAVAILABLE`; a real run selecting it is blocked and never silently falls back. No profile changes `MODEL_REGISTRY` or a workflow template.

## Node types i lifecycle

V0.2 obsługuje `IMPLEMENT`, `REVIEW`, `MACHINE_GATE`, `REPAIR`, `HUMAN_GATE` oraz opcjonalny `FINAL_GATE`. LLM node'y używają `DIRECT_CLI_CONTROL`; każdy startuje jako oddzielna, ephemeral sesja. Review działa w read-only sandbox i zapisuje `review_independence = SAME_PROVIDER_FRESH_CONTEXT`.

Przykład `WORKFLOWS/IMPLEMENT_REVIEW_REPAIR_V1.json` realizuje:

```text
IMPLEMENT -> MACHINE_GATE -> REVIEW -> HUMAN_GATE
                  |             |
                 FAIL          FAIL
                  +--> REPAIR <-+
                        |
                        +--> MACHINE_GATE
```

Po `REPAIR` zawsze następuje ponowny `MACHINE_GATE`, a potem `REVIEW`. Maksymalna liczba repair cycles wynosi 2.

## Limity i zatrzymanie

Każdy workflow deklaruje `max_nodes`, `max_repair_cycles`, `max_wall_time_minutes` i `max_llm_calls`; `max_token_budget` jest opcjonalny i pozostaje `null`, gdy nie ma niezawodnego rozliczania. Przekroczenie limitu kończy przebieg jako `WORKFLOW_LIMIT_REACHED` / `HUMAN_REQUIRED`. `BLOCKED`, `INVALID`, wadliwy wynik JSON, niejednoznaczne przejście, brak bindingu lub zmiana canonical checkout także natychmiast zatrzymują runner.

## Worktree policy

Repozytorium i execution worktree muszą istnieć, być czyste, należeć do tego samego Git common dir i wskazywać różne checkouty. `main_merge_allowed = false` jest wymagane przez walidator. Runner nie wykonuje merge, push, PR, reset ani usuwania worktree. Snapshot canonical `HEAD` i statusu jest sprawdzany przed i po każdym node.

## Handoff i Human Gate

Artefakty są zapisywane w `03_STATS/<AAW_RUN_ID>/WORKFLOW/`. `workflow_state.json` jest atomowo aktualizowany, a każdy wynik node'a otrzymuje osobny plik. Node package przenosi stan pracy (goal, worktree, acceptance, wyniki, diff/test evidence, issues i limity), nie historię rozmów.

Na Human Gate stan to `WAITING_FOR_HUMAN`. `ACCEPT CANDIDATE` zapisuje `human_verdict = ACCEPTED` oraz `READY_FOR_EXTERNAL_INTEGRATION`; nie scala zmian. `REJECT` zapisuje `REJECTED`, a `LEAVE FOR LATER` pozostawia workflow bez zmiany w `WAITING_FOR_HUMAN`.

## CLI

Walidacja i dry run:

```powershell
python workflow_runner.py --validate WORKFLOWS\IMPLEMENT_REVIEW_REPAIR_V1.json
python workflow_runner.py --workflow WORKFLOWS\IMPLEMENT_REVIEW_REPAIR_V1.json --goal "Napraw regresję importu CSV bez zmian poza wskazanym zakresem." --repo C:\path\to\repo --worktree C:\path\to\repo-worktree --dry-run
python workflow_runner.py --workflow WORKFLOWS\IMPLEMENT_REVIEW_REPAIR_V1.json --goal "Napraw regresję importu CSV bez zmian poza wskazanym zakresem." --repo C:\path\to\repo --worktree C:\path\to\repo-worktree --bind N01=TERRA_HIGH --bind N04=SOL_MEDIUM --dry-run
```

Realny workflow:

```powershell
python workflow_runner.py --workflow WORKFLOWS\IMPLEMENT_REVIEW_REPAIR_V1.json --goal "Napraw regresję importu CSV bez zmian poza wskazanym zakresem." --repo C:\path\to\repo --worktree C:\path\to\repo-worktree
```

Human verdict:

```powershell
python workflow_runner.py --run-id AAW_YYYYMMDD_HHMMSS_xxxxxxxx --human-verdict accept
```

## V0.2 limitations

Brak dynamicznego planowania, arbitrary DAG, adaptacyjnego doboru modeli, ORCA jako primary adapter, schedulerów, auto-merge/PR oraz multi-repo. Ogólne pause/resume po przerwaniu procesu jest deferred: V0.2 nie ryzykuje powtórnego wykonania node'a, którego side effects mogły wystąpić przed zapisem artefaktu. Human Gate jest trwały i może zostać rozstrzygnięty później na podstawie `workflow_state.json`.
