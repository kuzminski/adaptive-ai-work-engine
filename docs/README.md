# Dokumentacja Architektury i Specyfikacji AAW

Niniejszy katalog zawiera kompletną dokumentację architektoniczną, kontrakty wykonawcze oraz specyfikacje projektowe silnika **AAW (Adaptive AI Work Engine)**. Wszystkie specyfikacje techniczne znajdują się w podkatalogu [`specs/`](specs/).

---

## Roadmapa

- [`AAW_ROADMAP_V1.md`](specs/AAW_ROADMAP_V1.md) — Roadmapa rozbudowy funkcjonalnej po P3.0.1 i PR #6: diagnoza stanu, etapy 0–5 (konsolidacja, pętla dowodowa z prawdziwej pracy, od pomysłu do programu, wiele projektów, kontrolowana adaptacja, dojrzałość produktu) i wynikające z nich widoki UI/UX.

---

## Indeks specyfikacji technicznych

### 1. Architektura produktu i MVP
Specyfikacje dotyczące warstwy produktowej, interfejsu użytkownika końcowego, kontraktów identyfikacji i analityki.

- [`AAW_PRODUCT_MVP_V0_1.md`](specs/AAW_PRODUCT_MVP_V0_1.md) — Architektura pierwszej wersji produktu MVP (warstwa lokalna produktu dla użytkownika końcowego na zamrożonym silniku autonomii V0.3).
- [`AAW_PRODUCT_MVP_V0_2.md`](specs/AAW_PRODUCT_MVP_V0_2.md) — Wydanie i utwardzenie wersji produktowej MVP V0.2 (`0.3.0` / `0.4.0`: kreator pierwszego uruchomienia 7-kroków, sondowanie modeli, profile 5.5, pakiet portable).
- [`AAW_V0_4_ASTRA_DECISIONS.md`](specs/AAW_V0_4_ASTRA_DECISIONS.md) — Decyzje architektoniczne V0.4 (Astra decisions, wiarygodne uczenie pod obciążeniem selekcyjnym, progi governance).
- [`AAW_V0_4A_EXECUTION_ID_CONTRACT.md`](specs/AAW_V0_4A_EXECUTION_ID_CONTRACT.md) — Kontrakt tożsamości wykonania (`execution_id` jako unikalny lokalny identyfikator UUIDv4 pojedynczego wywołania).
- [`AAW_V0_4B_LEDGER_CONTRACT.md`](specs/AAW_V0_4B_LEDGER_CONTRACT.md) — Kontrakt dziennika cyklu życia wykonania (`execution_ledger` ze zdarzeniami faz i statusów).
- [`AAW_V0_4C_ANALYTICS_V2_CONTRACT.md`](specs/AAW_V0_4C_ANALYTICS_V2_CONTRACT.md) — Kontrakt analityki V2 (indeks analityczny, wskaźniki wydajności i agregaty sesji).

### 2. Autonomia i iteracje
Specyfikacje kontrolera autonomii, zarządzania pętlą wykonania i eskalacji implementatorów.

- [`AAW_AUTONOMOUS_ITERATIONS_V0_1.md`](specs/AAW_AUTONOMOUS_ITERATIONS_V0_1.md) — Warstwa autonomicznych iteracji V0.1 powyżej statycznego runnera przepływów (mandat, pętla wykonaj-zweryfikuj-napraw).
- [`AAW_AUTONOMOUS_ITERATIONS_V0_2.md`](specs/AAW_AUTONOMOUS_ITERATIONS_V0_2.md) — Integracja adapterów wykonawczych, blokada uruchomienia (`run lock`) i utwardzenie dowodów (evidence) V0.2.
- [`AAW_AUTONOMOUS_ITERATIONS_V0_3.md`](specs/AAW_AUTONOMOUS_ITERATIONS_V0_3.md) — Polityka modeli `DEFAULT_AUTONOMOUS` dla kontrolera autonomii V0.3 (zarządzanie profilami i katalogiem modeli).
- [`AAW_AUTONOMOUS_ITERATIONS_V0_3_FREEZE.md`](specs/AAW_AUTONOMOUS_ITERATIONS_V0_3_FREEZE.md) — Raport zamrożenia polityki autonomii (Freeze Report V0.3.2).
- [`AAW_LONG_AUTONOMY_V0_1.md`](specs/AAW_LONG_AUTONOMY_V0_1.md) — Długie pola formularzy i bezlicznikowa autonomia (count-free autonomy, praca do wyczerpania planu lub eskalacji).
- [`AAW_IMPLEMENTER_CHAIN_V0_1.md`](specs/AAW_IMPLEMENTER_CHAIN_V0_1.md) — Łańcuch implementatorów (uporządkowana sekwencja profili: najsłabszy pierwszy, deterministyczna eskalacja).
- [`AAW_IMPLEMENTER_EFFECTIVENESS_PLAN_V0_1.md`](specs/AAW_IMPLEMENTER_EFFECTIVENESS_PLAN_V0_1.md) — Plan podniesienia skuteczności implementatorów (kończenie iteracji, przydzielanie trudnych zadań silniejszym modelom).

- [`AAW_OPTIMIZATION_PROJECT_V0_1.md`](specs/AAW_OPTIMIZATION_PROJECT_V0_1.md) — Projekt optymalizacji: tryb łańcuchowy (długie serie iteracji z lekkim review, jedno poważne review na końcu serii, polerowanie), telemetria V1 i pasywny benchmark kosztu na zadanie.
- [`AAW_UX_REALTIME_EXPERIENCE_V0_1.md`](specs/AAW_UX_REALTIME_EXPERIENCE_V0_1.md) — Widok przebiegu na żywo, samobudujący się benchmark w układzie współrzędnych, rekomendacje „co byłoby optymalne” i prognoza „od pomysłu do końca w jednym ciągu”.

### 3. Planowanie i walidacja
Specyfikacje modułu planowania (Planner), generowania propozycji, naprawy pakietów i dyscypliny złożoności.

- [`AAW_PLANNER_PROPOSAL_PIPELINE_V0_1.md`](specs/AAW_PLANNER_PROPOSAL_PIPELINE_V0_1.md) — Zamrożony kontrakt potoku propozycji plannera V0.1 (generowanie strukturalnych planów zadań).
- [`AAW_PLANNER_LIVE_PROVIDER_VALIDATION_V0_2.md`](specs/AAW_PLANNER_LIVE_PROVIDER_VALIDATION_V0_2.md) — Walidacja kontraktu plannera na żywych dostawcach modeli i rzeczywistych przepływach V0.2.
- [`AAW_PLANNER_QUALITY_HARDENING_V0_3.md`](specs/AAW_PLANNER_QUALITY_HARDENING_V0_3.md) — Utwardzenie jakości i stabilności propozycji plannera V0.3.
- [`AAW_PLANNER_PACKAGE_REPAIR_V0_4.md`](specs/AAW_PLANNER_PACKAGE_REPAIR_V0_4.md) — Naprawa pakietu planowania i dyscyplina złożoności grafu (Smallest Sufficient Graph) V0.4.

### 4. Routing i modele
Specyfikacje wyznaczania tras zadań, obsługi limitów (quota) oraz ścieżkowego łączenia kontekstu.

- [`AAW_MULTIROUTING_RUNTIME_CONTRACT_V0_1.md`](specs/AAW_MULTIROUTING_RUNTIME_CONTRACT_V0_1.md) — Kontrakt wykonawczy multi-routingu (deterministyczne bramki, dynamiczny wybór krawędzi DAG).
- [`AAW_QUOTA_ROUTING_AND_REPAIR_ESCALATION_V0_1.md`](specs/AAW_QUOTA_ROUTING_AND_REPAIR_ESCALATION_V0_1.md) — Adaptacyjny routing modeli oraz ograniczona eskalacja napraw przy wyczerpaniu limitów (quota routing & repair escalation).
- [`AAW_PATH_SCOPED_BRANCH_CONTEXT_MERGE_V0_1.md`](specs/AAW_PATH_SCOPED_BRANCH_CONTEXT_MERGE_V0_1.md) — Ścieżkowy kontekst gałęzi w DAG oraz operacje merge/rejoin na podstawie wykonanych przodków.

### 5. Bezpieczeństwo i bramka człowieka
Specyfikacje dotyczące bezpieczeństwa, rejestrów ryzyka i bramek zatwierdzania przez człowieka.

- [`AAW_PRODUCTION_USE_GATE_V0_1.md`](specs/AAW_PRODUCTION_USE_GATE_V0_1.md) — Walidacja bramki wdrożenia produkcyjnego (Production Use Gate) z udziałem operatora.
- [`AAW_RISK_REGISTER_CHARTER_V0_1.md`](specs/AAW_RISK_REGISTER_CHARTER_V0_1.md) — Rejestr znanych ryzyk i wyznaczanie minimalnych progów ryzyka (risk floors) w karcie plannera.
- [`AAW_OPERATOR_RECOVERY_EDIT_SAFETY_V0_2.md`](specs/AAW_OPERATOR_RECOVERY_EDIT_SAFETY_V0_2.md) — Bezpieczeństwo ręcznych edycji przez operatora i procedury odzyskiwania stanu (recovery & edit safety).

### 6. UI i Canvas
Specyfikacje warstwy prezentacyjnej oraz mostu komunikacyjnego czasu rzeczywistego.

- [`AAW_CANVAS_FUNCTIONALIZATION_V0_1.md`](specs/AAW_CANVAS_FUNCTIONALIZATION_V0_1.md) — Funkcjonalizacja interaktywnego widoku Canvas (wizualizacja węzłów, stanów i wyników).
- [`AAW_UX_RUNTIME_BRIDGE_V0_1.md`](specs/AAW_UX_RUNTIME_BRIDGE_V0_1.md) — Most komunikacyjny między interfejsem użytkownika a procesem silnika (UX Runtime Bridge V0.1).
