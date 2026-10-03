# AAW Autonomous Iterations V0.3 — Freeze Report (V0.3.2)

## FINAL VERDICT

**AAW_DEFAULT_AUTONOMOUS_POLICY_V0_3_FROZEN**

Kryterium z zadania spełnione: świeży E2E dochodzi do realnego FINAL_REVIEW, następnie `ROADMAP_EXHAUSTED` → `AWAITING_HUMAN`.
Powtórzony niezależnym, drugim świeżym runem z identycznym przebiegiem.

## Flagi

| Flaga | Wartość |
|---|---|
| V0_3_STATE_COMMITTED | TRUE — commit `604266a` (stan V0.3) + commit poprawek V0.3.2 |
| FULL_REGRESSION_VERIFIED | TRUE — COLLECTED 447, PASSED 445, FAILED 0, SKIPPED 2 (dwa testy `real_stats`, wymagają realnych danych stats), 0 environment-only failures |
| CLEAN_POST_SCHEMA_FIX_E2E | TRUE — dwa niezależne świeże runy po poprawce schematu i poprawkach V0.3.2, bez `invalid_json_schema` |
| LUNA_IMPLEMENTATION_LIVE | TRUE — `GPT6_LUNA_HIGH` → `gpt-6-luna` / high |
| LUNA_PRETREATMENT_LIVE | TRUE — `GPT6_LUNA_VERY_HIGH` → `gpt-6-luna` / xhigh (przed primary i przed final review) |
| PRIMARY_REVIEW_SOL_6_1_LIVE | TRUE — `SOL_6_1_LIGHT` → `gpt-6.1-sol`, PASS |
| FINAL_REVIEW_SOL_5_6_LIVE | TRUE — `SOL_5_6_LIGHT` → `gpt-5.6-sol`, selection `DEFAULT_FINAL_REVIEW`, PASS |
| TARGETED_RAW_EVIDENCE_VERIFIED | TRUE — w obu runach i w obu reviewach: `RAW_EVIDENCE_REQUESTED` / `RAW_EVIDENCE_PROVIDED` dla `RAW_DIFF` i `EXECUTION_RESULT:*`, dane zwrócone z hashem |
| ROADMAP_EXHAUSTION_REACHES_HUMAN | TRUE — `hold.reason = ROADMAP_EXHAUSTED`, `roadmap.GREETING = DONE`, `promotable = true` |
| NO_SILENT_MODEL_SUBSTITUTION | TRUE — profil i runtime model w evidence zgodne z polityką dla każdej roli; Opus/Sonnet 5.5 = `PROFILE_UNAVAILABLE` |
| NO_AUTO_MERGE_PUSH | TRUE — `main_merge_allowed=false`; remote fixture zawiera tylko commit bazowy w obu runach |

## Diagnoza REVIEW_ESCALATED (LIVE_V03_22cd97370c23)

Eskalacja Sola 6.1 była **uzasadniona**, nie błąd modelu. Realna przyczyna: pakiet pokazywał rewiewerowi dwie różne wartości hasha tego samego diffa.

- `diff_sha256` (PACKET.access / RAW_METADATA) to hash kanoniczny kontrolera (`canonical_hash` nad JSON-owym opakowaniem tekstu, prefiks `sha256:`).
- `RAW_EVIDENCE_MANIFEST[RAW_DIFF].sha256` to hash bajtów pliku.
- Dodatkowo `Path.write_text` na Windows tłumaczył `\n` → `\r\n`, więc bajty pliku nie odpowiadały tekstowi diffa.
- Brak jakiegokolwiek opisu, że to dwa algorytmy. Reviewer słusznie uznał tożsamość artefaktu za nierozstrzygniętą.

Po poprawce (próby 1–2 V0.3.2 ujawniły kolejne dwie przyczyny w kontrakcie dowodu):

1. Pakowanie diffa: zapis bajt w bajt (`write_bytes`), jawne `diff_file_sha256` oraz `diff_digest_note` w `RAW_METADATA` opisujące różnicę algorytmów.
2. Kontrakt dowodu implementera: `required_evidence` jest dopasowywane po nazwie checka. Implementer odpowiadał czasem po polsku („Test jednostkowy”), co dawało FAIL, niepotrzebny repair i timeout. Instrukcja `execute` wymaga teraz angielskiej nazwy zgodnej z pozycją `REQUIRED_EVIDENCE` oraz podania w summary dokładnej komendy, exit code i wyników. Dopasowanie pozostaje ścisłe (fail-closed).
3. Fixture E2E: dodano `src/__init__.py` i `tests/__init__.py`, aby `unittest discover` działało (wcześniej test uruchamiany obejściem, a recenzent słusznie nie mógł zweryfikować pokrycia).

Nie wymuszono PASS, nie osłabiono eskalacji.

## Testy regresyjne dodane

- `test_32` — spójność i opis digestów diffa (bajty pliku = tekst diffa, `diff_file_sha256` = manifest, nota o różnych algorytmach). Odtwarza przyczynę `REVIEW_ESCALATED`.
- `test_33` — prawdziwie niejednoznaczny przypadek nadal eskaluje fail-closed (`REVIEW_ESCALATED`, `promotable=false`).
- `test_34` — instrukcja implementera o nazewnictwie dowodów i komendzie/exit code.

## Dowody E2E (`EVIDENCE/`)

| Plik | Wynik |
|---|---|
| `AAW_AUTONOMY_V0_3_2_ATTEMPT_01_REPAIR_TIMEOUT.json` | nieudana: FAIL dopasowania dowodu → repair → `EXECUTOR_FAILED` (timeout) |
| `AAW_AUTONOMY_V0_3_2_ATTEMPT_02_REVIEW_ESCALATED_EVIDENCE_CONTRACT.json` | `REVIEW_ESCALATED`: dowód testów tylko jako samoraport |
| `AAW_AUTONOMY_V0_3_2_ATTEMPT_03_FINAL_REVIEW_TIER_HARD_PROFILE_UNAVAILABLE.json` | primary PASS; implementer zgłosił niepewność środowiska → polityka wybrała tier hard (Sonnet 5.5) → `ROLE_PROFILE_UNAVAILABLE`, fail-closed bez podstawienia |
| `AAW_AUTONOMY_V0_3_2_FINAL_E2E.json` | **PASS**: pełny flow do `ROADMAP_EXHAUSTED` → `AWAITING_HUMAN` |
| `AAW_AUTONOMY_V0_3_2_REPEAT_E2E.json` | **PASS**: niezależne powtórzenie, identyczny przebieg |

Przebieg każdego z dwóch udanych runów: scripted initial planner → Luna HIGH implementation → deterministyczny self-verify → Luna VERY_HIGH pretreatment → Sol 6.1 Light primary review (ESCALATE z raw-evidence request → retrieval → PASS) → Luna VERY_HIGH pretreatment → Sol 5.6 Light final review (retrieval → PASS) → `ROADMAP_EXHAUSTED` → `AWAITING_HUMAN`. Repair nie był potrzebny i nie został wymuszony.

Dla każdego realnego wywołania: `execution_id`, `EXECUTION_INTENT` → `EXECUTION_STARTED` → `EXECUTION_CLOSED` w ledgerze, unikalna świeża sesja providera, profil i `selection_reason` są w `summary.executions` obu plików. Initial planner (scripted) ma INTENT + CLOSED i celowo brak STARTED (brak procesu; nic nie sfabrykowano). Pretreatment zwraca wyłącznie pola `summary / implementation_claims / check_refs / finding_refs / changed_files / source_refs`, bez werdyktu.

## Ograniczenia i uwagi (jawnie)

- Initial planner jest scripted wyłącznie dlatego, że dokładny Opus 5.5 = `PROFILE_UNAVAILABLE`; jego live walidacja NIE jest deklarowana. Sonnet 5.5 (capability escalation / final review hard) i Opus 5.5 MEDIUM (critical) również `PROFILE_UNAVAILABLE`.
- Powtarzalność: 2 z 2 runów po ostatnich poprawkach przeszły, ale próba 3 pokazała, że implementer może zgłosić niepewność środowiskową (Codex PowerShell nie ustawił cwd na worktree). Polityka wtedy poprawnie podnosi final review do tieru hard i, przy niedostępnym Sonnecie 5.5, zatrzymuje się fail-closed. To zachowanie zgodne z polityką, ale zależy od nondeterministycznej odpowiedzi modelu. Do domknięcia w przyszłości: mapowanie Sonnet 5.5 w MODEL_CATALOG, gdy runtime będzie dostępny.
- Ścieżki wyczerpania roadmapy z wieloma iteracjami, scope-creep escalation i Sonnet capability escalation pokryte testami jednostkowymi, nie live.
- Nie wykonano merge ani push.
