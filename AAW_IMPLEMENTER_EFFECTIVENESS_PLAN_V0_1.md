# AAW — plan naprawy skuteczności implementacji (V0.1)

Cel: żeby słabsze modele (GPT-6 Luna high/max, Gemini Flash, inne) **kończyły** iteracje
bez niekończących się poprawek, a zadania widocznie trudne od razu dostawał silniejszy model
(GPT-6.1 Sol medium lub inny). Lifecycle silnika bez zmian:
`PLAN → EXECUTE → SELF_VERIFY → REVIEW → REPAIR → FINAL_REVIEW → Human Gate`.

Status: **faza 1 wdrożona** w tym commicie (kod + testy). Fazy 2–3 poniżej to plan.

---

## 1. Diagnoza — dlaczego AAW był nieefektywny

| # | Problem (potwierdzony w kodzie / dowodach) | Skutek |
|---|---|---|
| D1 | Plan iteracji = `goal` + `acceptance_criteria` + `touched_areas`. Brak plików, kroków, komend weryfikacji. Implementator dostawał „Implement exactly PLAN”. | Słabszy model sam planował w swoim wywołaniu, zgadywał pliki, pomijał testy. |
| D2 | Dowody dopasowywane do `REQUIRED_EVIDENCE` podciągiem nazwy checku; implementator musiał „zgadnąć” nazwę. | Fałszywe `SELF_VERIFY::<dowód>` FAIL → pętle napraw (przypadek AC8 z `AAW_QUOTA_ROUTING_AND_REPAIR_ESCALATION_V0_1.md` §3). |
| D3 | Brak samokontroli implementatora na końcu wywołania. | Proste błędy (składnia, brak uruchomionego testu, zła lista plików) wychodziły dopiero w płatnym review. |
| D4 | Drabina napraw: Luna xhigh → **znowu Luna** (max) → dopiero Sonnet. Każda runda = self-verify + pretreatment + review. | Dwie długie próby tej samej słabej rodziny modeli zanim trafi do silniejszego. |
| D5 | `review_pretreatment` (Luna xhigh) wołany przy **każdym** review i final review, choć nie może zmienić werdyktu (docstring `build_direct_executors` mówił, że jest wyłączony — nie był). | +2 wywołania modelu na iterację bez wpływu na wynik (test real-adapter: 7 → 5 wywołań). |
| D6 | Kolejne iteracje planował profil final review DEFAULT = **Sol 5.6 light (low)**. | Najsłabszy model w łańcuchu pisał plan dla słabego implementatora. |
| D7 | `SIGNIFICANTLY_DIFFICULT` → Luna max; brak routingu „widocznie trudne → silny model od razu”. | Trudne zadania szły do Luny i kończyły w drabinie napraw. |
| D8 | Gemini/Antigravity: tylko kontrakt adaptera, brak wywołania. | Brak trzeciego providera (limity, niezależne review). |
| D9 | Router quota nie sprawdza alternatyw z góry — mógłby wybrać niesprawdzony profil. | Ryzyko przy dodaniu Gemini. |

## 2. Zasady

1. **Plan jest instrukcją wykonawczą, nie opisem celu.** Planista (silny model) pisze pakiet
   pracy tak, by implementator nie musiał planować.
2. **Najtańszy moment na wykrycie błędu to koniec tego samego wywołania** (model ma jeszcze
   kontekst) → obowiązkowy krótki audyt końcowy.
3. **Najprostsze błędy wykrywa kod, nie model** (składnia, JSON, markery konfliktu, „gotowe”
   bez zmian).
4. **Słaby model nie dostaje drugiej, dłuższej szansy na to samo.** Widocznie trudne → silny
   model od razu; nieudana naprawa słabego → silny model ze świeżym kontekstem i diagnozą.
5. **Uczciwy FAIL jest tani, fałszywy PASS drogi** — instrukcje i schematy to wymuszają.
6. Zero cichych podmian: każdy wybór modelu jest w `MODEL_POLICY_SELECTED` / ledgerze.

## 3. Faza 1 — wdrożone teraz

| Problem | Zmiana | Gdzie | Test |
|---|---|---|---|
| D1 | `work_packet` obowiązkowy w planie ITERATION: `files_to_read`, `files_to_change`, 1–6 kroków (akcja, pliki, szczegóły, weryfikacja kroku), `verification_commands` (komenda + `evidence_name` = dosłownie `REQUIRED_EVIDENCE` + oczekiwany wynik), `definition_of_done`, `pitfalls`, `out_of_scope`, `needs_strong_implementer`. Planista dostaje `REQUIRED_EVIDENCE` i reguły pisania dla słabszego implementatora. Lint pakietu (`WORK_PACKET_ASSESSED` w journalu). | `work_packet.py`, `autonomy_adapters.py` (schemat `plan`, instrukcje, handoff) | `test_implementer_effectiveness.py` |
| D2 | Tolerancyjne dopasowanie nazw dowodów (wszystkie istotne tokeny); planista mapuje komendy na nazwy dowodów. | `work_packet.evidence_matches`, `autonomy_controller._deterministic_self_verification` | j.w. |
| D3 | **Audyt końcowy implementatora** (execute i repair): checklista A1–A7 (DoD spełnione, komendy uruchomione po ostatniej edycji, składnia, referencje/importy, brak pozostałości, zakres, raport plików). Wynik `self_audit` w schemacie; FAIL → tania naprawa **przed** review; brak audytu → WARN dla reviewera. Stabilne nazwy wierszy, więc późniejszy PASS zastępuje FAIL (brak pętli). | `work_packet.AUDIT_CHECKLIST`, `audit_checks`; `autonomy_controller._record_self_audit` | j.w. |
| D3 | **Deterministyczne kontrole prostych błędów** w SELF_VERIFY: markery konfliktu, składnia Python (kompilacja w pamięci), składnia JSON, „brak zmian mimo listy plików” (FAIL); pliki zgłoszone a niezmienione, zmiany poza pakietem (WARN). | `work_packet.static_sanity_checks`, `autonomy_controller._static_sanity_rows` | j.w. |
| D4 | Domyślna drabina: `CURRENT` (Luna) → **`DIFFICULT_IMPLEMENTER` = Sol 6.1 medium** (diagnoza najpierw) → `PLANNER_DIAGNOSIS` → Human Gate. `EFFORT_UP` pominięty (`max_effort_steps: 0`). Praca, którą robił silny implementator, nie trafia do słabszego naprawiającego. | `AUTONOMY_ROLES.json`, `autonomy_policy.default_repair_escalation`, `select_repair` | `test_repair_escalation.py`, `test_autonomy.py` |
| D5 | Pretreatment wyłączony domyślnie (`build_direct_executors(review_pretreatment=False)`). | `autonomy_adapters.py` | `test_autonomy_v0_2.py` |
| D6 | Nowy slot `continuation_planner` = **Sol 6.1 medium** dla tieru DEFAULT (HARD/CRITICAL bez zmian). | `autonomy_policy.select_continuation_planner`, `AUTONOMY_ROLES.json`, `MODEL_RECOMMENDATIONS.json` | `test_autonomy_policy_v0_3.py` |
| D7 | Nowy slot `implementer_strong` = **Sol 6.1 medium**. Trasa STRONG gdy: `SIGNIFICANTLY_DIFFICULT`, planista prosi (`needs_strong_implementer` z powodem), >6 kroków lub >6 plików, >3 katalogi, albo pakiet strukturalnie niekompletny. Trasa HARDER (Luna xhigh) dla średnich lub mglistych pakietów. | `work_packet.assess_difficulty`, `autonomy_policy.select_implementation` | j.w. |
| D8 | **Antigravity CLI (`agy`)** jako trzeci harness (następca Gemini CLI): profile `AGY_GEMINI_3_1_PRO` (`gemini-3.1-pro-high`) i `AGY_GEMINI_FLASH` (`gemini-3.7-flash-high`); wykrywanie w aplikacji, status logowania, „Sprawdź modele”, wywołanie ról z `--json-schema`, parsowanie koperty JSON. Późna alternatywa w rekomendacjach (gdy brak Codex/Claude). | `autonomy_adapters.py`, `product_providers.py`, katalogi JSON | `test_implementer_effectiveness.py`, `test_product_mvp*.py` |
| D9 | Aplikacja oddaje routerowi jako alternatywy **tylko profile uruchamialne na tym komputerze**; Antigravity w `AUTONOMY_ROLES.json` domyślnie `available: false`, włączany po pozytywnym „Sprawdź modele”. | `product_recommendations._routing_for_machine` | j.w. |

Zgodność wsteczna: run zamrożony przed tą zmianą (bez slotów `implementer_strong` /
`continuation_planner`) zachowuje stare zachowanie (fallback do `implementer_hard` /
`final_review_default`, stara drabina). Plan bez klucza `work_packet` nie jest traktowany
jako „niedookreślony”.

### Przepływ iteracji po zmianie

```
PLAN (Opus 5.5 high / Sol 6.1 medium) ── work_packet ──► lint + ocena trudności
        │                                                   │
        │                     DEFAULT ─► Luna high   HARDER ─► Luna xhigh   STRONG ─► Sol 6.1 medium
        ▼
EXECUTE: kroki pakietu → komendy weryfikacji → AUDYT KOŃCOWY (A1–A7) → wynik + self_audit
        ▼
SELF_VERIFY (deterministycznie, bez modelu): dowody vs REQUIRED_EVIDENCE, audyt FAIL?,
        składnia .py/.json, konflikty, „brak zmian”  ── FAIL ──► REPAIR (tania, przed review)
        ▼
REVIEW (Sol 6.1 light) ── REPAIR_REQUIRED ──► REPAIR: Luna → [nie zbiega] → Sol 6.1 medium
        ▼                                         (diagnoza) → diagnoza planisty → Human Gate
FINAL REVIEW → kolejna iteracja albo Human Gate
```

### Antigravity CLI (`agy`) — jak włączyć

Gemini CLI został wyłączony przez Google (18.06.2026) i zastąpiony przez Antigravity CLI (`agy`);
AAW korzysta z `agy` (wsparcie Gemini CLI usunięte).

1. Instalacja: PowerShell `irm https://antigravity.google/cli/install.ps1 | iex`
   (macOS/Linux: `curl -fsSL https://antigravity.google/cli/install.sh | bash`).
2. Raz w terminalu: `agy` → logowanie kontem Google.
3. W AAW: „Wykryj ponownie” → „Sprawdź modele” (jedno krótkie zapytanie na model).
4. Profile `AGY_GEMINI_3_1_PRO` (`gemini-3.1-pro-high`) i `AGY_GEMINI_FLASH`
   (`gemini-3.7-flash-high`) są dostępne dopiero po akceptacji modelu na tym komputerze.

Wywołanie: `agy --model <slug> --output-format json --json-schema <plik> [--dangerously-skip-permissions]
--print "Read the file <temp>/aaw_role_prompt.md …"`. Wszystkie opcje przed `--print` (agy traktuje
resztę jako prompt). W trybie `-p` agy nie czyta stdin, a handoff z diffem przekracza limit linii
poleceń Windows, więc pełny prompt roli trafia do pliku w katalogu tymczasowym systemu (agy domyślnie
ma do niego dostęp). Role tylko do odczytu działają w domyślnym trybie uprawnień (odczyt workspace
dozwolony, edycje i komendy wymagają zgody, której w trybie headless nikt nie da → odmowa); role
piszące dostają `--dangerously-skip-permissions` (shell do testów; granice Git i tak sprawdza
kontroler po fazie). Wynik: koperta `{conversation_id, status, response, usage, structured_output}`;
`structured_output` wymusza `--json-schema`, a odpowiedź jest dodatkowo walidowana (wymagane pola) —
brakujące pola = odrzucenie, nigdy cichy PASS. Status logowania: `agy -p /usage --output-format json`
(bez tury agenta i bez zużycia limitu). Trust: `SECONDARY` (nie dla krytycznych review).

## 4. Faza 2 — następne kroki (po zebraniu danych z prawdziwych runów)

1. **Metryki skuteczności** w widoku produktu i ewidencji: wywołania modeli na iterację,
   naprawy na iterację, odsetek iteracji zakończonych bez naprawy, odsetek audytów z FAIL,
   ile napraw złapał audyt/kontrole statyczne przed review, czas do Human Gate.
   Bez tego nie da się uczciwie ocenić progów trudności (6 kroków / 6 plików / 3 katalogi).
2. **Jedna poprawka planu zamiast silnego implementatora**, gdy lint pakietu ma tylko
   „miękkie” braki (np. brak `verify` w kroku): krótka poprawka przez planistę jest tańsza
   niż silny implementator.
3. **Pakiet pracy w UI**: brief fazy EXECUTE pokazuje kroki, komendy i wynik audytu.
4. **Limit tur na profil** (`max_turns`) — krótszy dla słabych modeli, żeby nie „błądziły”.
5. **Statyczne workflow** (`WORKFLOWS/*.json`, węzły IMPLEMENT): ten sam pakiet pracy i audyt.

## 5. Faza 3 — dla bardzo słabych modeli

1. **Wykonanie krok-po-kroku**: jedno wywołanie na krok pakietu (krótki kontekst, weryfikacja
   po kroku) zamiast jednego długiego wątku.
2. Uczenie progów trudności z metryk fazy 2 (per projekt).

## 6. Ograniczenia i niepewność (uczciwie)

- Zmiany zweryfikowane testami deterministycznymi i fałszywymi CLI (Claude/Antigravity). **Żaden
  prawdziwy provider nie był uruchomiony** w tym środowisku (brak zalogowanych CLI).
- Flagi `agy` pochodzą z oficjalnego changelogu Antigravity CLI (1.2.17) i integracji spec-kit; nie
  były uruchomione na prawdziwym `agy`. Slugi modeli (`gemini-3.1-pro-high`, `gemini-3.7-flash-high`)
  pochodzą z listy `agy models`; starsze wersje `agy` potrafiły po cichu podmienić slug na inny
  model (zgłoszenia #581/#687) — dlatego wymagane jest „Sprawdź modele” i aktualne `agy`.
- `SOL_6_1_MEDIUM` (`gpt-6.1-sol` / `medium`) korzysta z istniejącego wpisu katalogu
  (DYNAMIC_PREFLIGHT); nie był sondowany na żywo tutaj.
- Progi trudności są heurystyką startową; do kalibracji w fazie 2.
