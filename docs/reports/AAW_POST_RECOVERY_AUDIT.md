# Niezależny Audyt Powykonawczy Mandatu AAW P0–P2
**Data audytu:** 2026-11-02  
**Badana gałąź:** `recovery/p0-p2-hardening` (commit: `572d562`)  
**Baza odniesienia:** `origin/main` (commit: `f2eafce853`)  
**Środowisko:** Windows (PowerShell, Python 3.14.7, Node.js 20+, pytest 9.1.1)  
**Tryb audytu:** Ścisły, bezstronny, empiryczny — zero sfabrykowanych metryk, wyraźny podział na testy rzeczywiste i symulowane.

---

## 1. Pełna regresja testów projektu

- **Status:** **PASS** (z 2 uzasadnionymi SKIP środowiskowymi)
- **Zakres:** Uruchomiono całościową regresję wszystkich plików testowych w repozytorium:
  `python -m pytest recovery_worktree -q -p no:cacheprovider`
- **Liczba wykrytych unikalnych testów:** **932 testy** w 40 modułach testowych.
- **Czas trwania:** **915.84s (15 minut 15 sekund)**.
- **Wynik końcowy:**
  - **PASSED:** **930**
  - **FAILED:** **0**
  - **SKIPPED:** **2**

### Szczegółowa analiza pominiętych testów (SKIP):
Oba pominięcia wystąpiły w module `CONTROL_CENTER/ANALYTICS/test_aaw_analytics.py`:
1. `test_real_stats_rebuild_is_non_destructive_and_deterministic`
   - *Powód:* `pytest.skip("03_STATS not present on this machine")`
   - *Uzasadnienie:* Brak lokalnego katalogu danych statystycznych `03_STATS` na stacji deweloperskiej; test pomija się celowo, nie ukrywając błędu.
2. `test_real_stats_refresh_after_rebuild_changes_nothing`
   - *Powód:* `pytest.skip("03_STATS not present on this machine")`
   - *Uzasadnienie:* Identycznie jak wyżej.

Żadne inne testy nie zgłosiły błędów ani nieoczekiwanych ostrzeżeń blokujących.

---

## 2. Weryfikacja Frontendu i Artefaktów Pakowania

- **Status:**
  - `npm ci`, `npm test`, `npm run build`: **PASS**
  - Artefakty produkcyjne frontendu: **PASS**
  - Paczka binarna Windows (`AAW-Windows-x64.zip`): **UNVERIFIED**

### Przebieg i dowody empiryczne:
1. **`npm ci`:**
   - Wykonano w `recovery_worktree/frontend`.
   - Wynik: zainstalowano 140 pakietów w 21s, kod wyjścia `0`.
2. **`npm test`:**
   - Wykonano: `npm --prefix recovery_worktree/frontend test -- --run`.
   - Przetestowano 25 przypadków testowych w 6 pakietach (`unitTests.ts`):
     - `ApiClient Token & Auth Guard` (testy 1–2)
     - `Lifecycle Classifier & EngineStore` (testy 3–4)
     - `StudioLayout, Navigation & MCP Modal` (testy 5–8)
     - `7-Phase Execution Graph & SVG Loopback` (testy 9–11)
     - `Human Gate Banner, Keyboard Shortcuts & Modal` (testy 12–16)
     - `Telemetry Terminal, Diff Viewer & Evidence Ledger` (testy 17–20)
     - `LiveCommandCenter, NewTaskWizard, RunsHistory & Settings` (testy 21–25)
   - Wynik: **25/25 zaliczonych (100% PASS)**.
3. **`npm run build`:**
   - Wykonano: `npm --prefix recovery_worktree/frontend run build` (`tsc && vite build`).
   - Przetransformowano 1610 modułów w 5.09s.
   - Wygenerowano produkcyjne artefakty w `PRODUCT_UI/`:
     - `PRODUCT_UI/index.html` (0.60 kB)
     - `PRODUCT_UI/assets/index-CIqX9Uch.css` (36.17 kB)
     - `PRODUCT_UI/assets/index-BzQFho6-.js` (300.48 kB)
   - Serwowanie statyczne zweryfikowano testem `tests/test_product_server_static.py` (**6/6 zaliczonych**).
4. **Paczka Windows (`build_portable.py`):**
   - W lokalnym środowisku Python brak modułu PyInstaller (`ModuleNotFoundError: No module named 'PyInstaller'`).
   - Zgodnie z wytycznymi mandatu i dokumentacją w `packaging/build_portable.py`, krok ten jest przeznaczony dla środowiska CI (`.github/workflows/aaw-portable.yml`), gdzie instalacja PyInstaller i budowa archiwum `.zip` odbywa się automatycznie.
   - Zgodnie z dyspozycją użytkownika punkt ten oznaczono jednoznacznie jako **UNVERIFIED**.

---

## 3. Test E2E przez Rzeczywisty Interfejs Aplikacji

- **Status:** **PASS**
- **Zakres:** Sprawdzenie przepływu E2E: utworzenie zadania, START, wykonanie etapów, STOP, RESUME i obsługa Human Gate.
- **Dowód:** Uruchomiono i zweryfikowano pełny zestaw testów integracyjnych w `test_product_mvp.py`:
  - `test_start_to_human_gate_with_briefs_timeline_and_no_merge_push`:
    Tworzy zadanie przez endpoint API, uruchamia proces roboczy (`AAW --run-worker`), przechodzi przez fazy autonomii (`PLAN` → `IMPLEMENT` → `REVIEW`), dochodzi do stanu zatrzymania `AWAITING_HUMAN` i weryfikuje brak nieautoryzowanych modyfikacji gałęzi `main`.
  - `test_stop_during_execute_waits_then_pauses_and_resume_completes`:
    Symuluje kliknięcie przycisku **STOP SAFELY** podczas trwającego wykonania, poprawnie zatrzymuje worker, zwalnia blokadę biegu (`lock_token`), przechodzi w stan `PAUSED`, a następnie przez żądanie `/resume` wznawia zadanie i doprowadza je do końca.
  - `test_accept_reject_and_continue_never_merge_or_push`:
    Testuje wszystkie 3 decyzje Human Gate: `ACCEPT`, `REJECT` oraz `CONTINUE`. Potwierdza, że żadna z nich nie dokonuje automatycznego `git merge` do gałęzi canonical ani `git push`.
- **Wynik:** Wszystkie 3 scenariusze E2E zaliczone w **56.25s**.

---

## 4. Weryfikacja Integracji `adaptive_orchestrator.py` z Normalną Ścieżką Wykonania

- **Status:** **FAIL (Brak integracji z runtime)**
- **Weryfikacja kodu źródłowego:**
  - Przeszukano całe repozytorium pod kątem wywołań modułu:
    `git grep -i "adaptive_orchestrator"`
  - Moduł `adaptive_orchestrator.py` występuje **wyłącznie** w:
    1. Samym pliku `adaptive_orchestrator.py`
    2. Pliku testowym `test_adaptive_orchestration_p2.py`
    3. Wcześniejszym raporcie podsumowującym fazę P2.
  - **Żaden plik wykonawczy aplikacji** (`workflow_runner.py`, `product_runs.py`, `autonomy_controller.py`, `aaw_run_v0_1.py`, `product_server.py`) **nie importuje ani nie wywołuje** funkcji `recommend_strategy` ani `generate_adaptive_dag`.
- **Wnioski audytu:**
  - W rzeczywistym runtime zadania uruchamiane przez interfejs użytkownika trafiają do `product_runs.py:start_task`, który uruchamia `autonomy_controller.py` wykonujący sztywną, sekwencyjną pętlę 7 faz (lub statyczne szablony z katalogu `WORKFLOWS/`).
  - Dynamiczne generowanie topologii DAG oraz automatyczny dobór wzorca równoległego na podstawie nakładania się plików nie zostały jeszcze zintegrowane z głównym punktem wejścia aplikacji. Moduł `adaptive_orchestrator.py` działa wyłącznie w ramach dedykowanego testu jednostkowo-integracyjnego.

---

## 5. Równoległe Podzadania, Izolacja, MERGE i Wznowienie

- **Status:** **PARTIAL / UNVERIFIED dla fizycznej współbieżności OS**
- **Analiza mechanizmu współbieżności:**
  - W `routing_contract.py` (linia 679) zapisano fundamentalną regułę silnika:
    > *"this runner has one FIFO frontier and no real concurrency"*
  - W `workflow_runner.py` pętla wykonawcza iteruje po węzłach sekwencyjnie (`while frontier:`).
  - W przypadku rozgałęzienia `ALL_MATCHES` (np. węzły `N05` i `N06` po bramce maszynowej):
    - Oba węzły są dodawane do kolejki FIFO.
    - Silnik wykonuje najpierw węzeł pierwszy, a po jego zakończeniu węzeł drugi.
    - Następnie węzeł `MERGE` z polityką `ALL_REQUIRED` łączy ich wyniki.
  - Oznacza to, że **izolacja gałęziowa i semantyczne scalanie wyników (MERGE) działają poprawnie**, ale jest to **współbieżność logiczna/grafowa**, a **nie fizyczna wieloprocesowość** realizowana jednocześnie w tym samym ułamku sekundy.
  - Testy w `test_adaptive_orchestration_p2.py` obliczają czas równoległy wzorem matematycznym (`max(t1, t2) + overhead`), a w `test_path_scoped_branch_context.py` izolację weryfikuje się na mocku adaptera LLM. Brak jest w repozytorium testu uruchamiającego dwa równoległe procesy robocze CLI w dwóch osobnych worktree w tym samym czasie zegarowym.

---

## 6. Weryfikacja Benchmarku P1 (Rzeczywiste Wykonania vs Symulacja)

- **Status:** **PARTIAL / PROJEKCJA MODELOWA (Brak pomiarów live API)**
- **Analiza implementacji `benchmark_harness.py`:**
  - Funkcja `simulate_task_execution`:
    - Działa w trybie syntetycznym: `is_synthetic_cli: bool = True`.
    - Czas wykonania jest generowany formułą: `duration_ms = ... + (calls * 45.0)`.
    - Zużycie tokenów i szacowany koszt bazują na stałych mnożnikach:
      `cost_usd = round((total_tokens / 1000.0) * cost_per_1k, 5)`.
    - Nie wykonano rzeczywistych zapytań HTTP do zewnętrznych interfejsów OpenAI, Anthropic czy Google Gemini.
- **Wnioski audytu:**
  - **Bezwzględnie udowodniona korzyść:** Redukcja liczby wywołań modeli o **-20.0%** (5 zadań klasy TRIVIAL zostaje w 100% obsłużonych deterministycznie przez kod Pythona z pominięciem LLM, co realnie redukuje koszt tych operacji do 0).
  - **Szacunek teoretyczny:** Oszczędność kosztowa rzędu **84.1%** wynika z przemnożenia szacowanych tokenów przez teoretyczny cennik profili (Gemini Flash vs Opus), a nie ze zmierzonego zużycia w produkcyjnych billingach dostawców. Zgodnie z wymogiem audytu, wynik ten traktujemy jako kalkulację analityczną, a nie dowód fizycznej jakości wygenerowanego kodu.

---

## 7. Krytyczny Przegląd MCP i Niezmienników Bezpieczeństwa

- **Status:** **PASS**
- **Weryfikacja zabezpieczeń:**
  1. **Naruszenia Human Gate:**
     - Żadne narzędzie MCP nie pozwala na ominięcie lub automatyczną akceptację Human Gate.
     - Próby wywołania niedozwolonych akcji (`aaw_approve`, `aaw_accept`, `aaw_gate_decision`) są blokowane na poziomie serwera i zwracają kod błędu (potwierdzone 10 testami negatywnymi w `test_aaw_mcp.py`).
  2. **Git Containment:**
     - Blokada modyfikacji canonical `main` jest egzekwowana przez funkcję `assert_main_unchanged` oraz walidację przestrzeni roboczej `validate_workspace`.
     - Narzędzia `aaw_git_merge` i `aaw_git_push` są zablokowane na serwerze MCP.
  3. **Autoryzacja i transport sieciowy:**
     - Wszelkie żądania HTTP do serwera MCP bez ważnego tokenu (`X-AAW-Token` / `Bearer`) są odrzucane ze statusem 401 Unauthorized.
     - Serwer akceptuje zapytania wyłącznie na lokalnych interfejsach loopback (`127.0.0.1`, `localhost`, `[::1]`) i odrzuca żądania z obcym nagłówkiem Origin.
     - Wprowadzono twardy limit wielkości żądania (2MB) zapobiegający atakom DoS.
     - Parametry identyfikatorów przebiegów podlegają ścisłej walidacji wyrażeniem regularnym `^[a-zA-Z0-9_\-]+$`, co zapobiega atakom typu path traversal.
  - **Wynik:** Wszystkie 36 testów w `test_aaw_mcp.py` zaliczone w **7.52s**.

---

## 8. Tabela Zbiorcza Wyników Audytu

| Obszar Audytu | Wymaganie Mandatu | Wynik Audytu | Dowód |
|:---|:---|:---:|:---|
| **1. Regresja pełna** | 100% testów repozytorium | **PASS** | 930 passed, 2 skipped (0 failed) w 915.84s |
| **2. Frontend** | `npm ci`, `npm test`, `npm run build` | **PASS** | 25/25 testów unit, build w 5.09s, assety w `PRODUCT_UI/` |
| **2b. Paczka Windows** | Budowa i test `.zip` z EXE | **UNVERIFIED** | Brak PyInstaller w środowisku lokalnym (krok CI) |
| **3. Przepływ E2E** | START → EXEC → STOP/RESUME → GATE | **PASS** | `test_product_mvp.py` (3/3 testy w 56.25s) |
| **4. Integracja P2** | `adaptive_orchestrator` w runtime | **FAIL** | Moduł istnieje, ale nie jest wywoływany w `product_runs`/`workflow_runner` |
| **5. Równoległość P2** | Wykonanie współbieżne podzadań | **PARTIAL** | Logiczna izolacja gałęzi działa; silnik przetwarza węzły w kolejce FIFO |
| **6. Benchmark P1** | Rzeczywiste vs szacowane koszty | **PARTIAL** | Redukcja wywołań TRIVIAL (-20%) realna; koszt 84.1% to estymacja modelowa |
| **7. Bezpieczeństwo MCP** | Token, loopback, Human Gate, Git | **PASS** | 36/36 testów `test_aaw_mcp.py` zaliczonych, fail-closed potwierdzone |

---

## 9. Jednoznaczne Odpowiedzi na Pytania Kluczowe

### 1. Czy P0 można zamknąć?
> **TAK (PASS).**  
> Wszystkie krytyczne luki fazy P0 zostały bezspornie usunięte: CI zostało ustabilizowane i rozszerzone o frontend, Agentic Studio posiada kompletne widoki i dynamiczną nawigację, serwer MCP został rygorystycznie zabezpieczony (token, loopback, origin, payload cap, brak auto-approvalu), a pełna regresja przeszła bez ani jednego błędu.

### 2. Czy P1 przynosi udowodnione korzyści?
> **TAK, W ZAKRESIE REDUKCJI WYWOŁAŃ (PASS CZĘŚCIOWY).**  
> Deterministyczny bypass TRIVIAL bezsprzecznie eliminuje 100% zbędnych wywołań LLM dla czynności mechanicznych (zmniejszenie liczby zapytań o 20% w benchmarku). Natomiast deklarowane 84.1% oszczędności finansowych opiera się na teoretycznym kalkulatorze cennikowym profili, a nie na zmierzonych fakturach z live API.

### 3. Czy P2 działa w rzeczywistym runtime?
> **NIE (FAIL W RUNTIME / PROTOTYP SPECYFIKACYJNY).**  
> Moduł `adaptive_orchestrator.py` został poprawnie zaimplementowany i przetestowany w izolacji, ale **nie został wpięty w normalny przepływ uruchamiania zadań w `product_runs.py` ani `autonomy_controller.py`**. Ponadto silnik `workflow_runner` przetwarza węzły sekwencyjnie z kolejki FIFO, realizując równoległość logiczną (gałęzie Git), a nie fizyczną współbieżność procesów OS.

### 4. Czy gałąź kwalifikuje się do merge do main?
> **WARUNKOWO — WYMAGA ŚWIADOMEJ DECYZJI OPERATORA (HOLD).**  
> - **Co jest gotowe do `main`:** Cała stabilizacja P0 (CI, frontend, security MCP) oraz P1.1 (routing modeli, klasy zadań, budżety, bypass TRIVIAL) — kod jest wysoce stabilny i przeszedł 930 testów regresyjnych.  
> - **Co blokuje pełny merge P2:** Moduł `adaptive_orchestrator.py` wymaga formalnego zintegrowania z kontrolerem autonomii, jeśli orkiestracja adaptacyjna ma być dostępna dla użytkownika końcowego z poziomu UI, a nie tylko w testach.  
> - **Rekomendacja:** Gałąź `recovery/p0-p2-hardening` powinna pozostać w obecnym stanie w worktree do czasu podjęcia decyzji przez właściciela projektu o ewentualnym wdrożeniu integracji P2 lub selektywnym scaleniu hardeningu P0/P1. Zgodnie z zasadą fail-closed **nie wykonano merge ani push**.
