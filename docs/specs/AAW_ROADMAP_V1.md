# AAW — roadmapa rozbudowy funkcjonalnej V1

**Data:** 2026-11-03 · **Baza:** `main@579d582` + PR #6 (`claude/aaw-optimization-telemetry-e6b52c`, scalony z `main`)
**Podstawa:** pełne rozpoznanie kodu, testów, CI i dokumentacji (4 przeglądy: silnik, produkt/UI, dokumentacja/raporty, testy/CI/benchmarki), raport przekazania `AAW_CURRENT_STATUS_HANDOVER_FOR_CLAUDE.md`.

---

## 1. Diagnoza — gdzie AAW jest naprawdę

**Mocne strony (zweryfikowane w kodzie i CI):**
- Jedna produkcyjna ścieżka autonomii (`autonomy_controller` + `autonomy_adapters`): PLAN → EXECUTE → SELF_VERIFY → REVIEW → REPAIR → FINAL_REVIEW → ROADMAP_CHECK, Human Gate jako jedyna droga do integracji, izolacja w worktree, ledger wykonań, blokada runu, STOP SAFELY / STOP NOW / RESUME bez ślepego powtarzania efektów.
- Bezpieczeństwo transakcyjne (P2.4/P3.0.1): fail-closed merge z dziennikiem `fsync`, odzyskiwanie idempotentne, Human Gate promowalny tylko przy `PASS` + `verified`.
- Tryb łańcuchowy, telemetria per wywołanie, pasywny benchmark z prawdziwych runów, kontrolowana eksploracja i rozpis pomysłu (PR #6) — działają w backendzie, z testami.
- Nowe Studio (React) z grafem faz, dziennikiem dowodów, diffem i Human Gate; serwer MCP z 8 bezpiecznymi narzędziami (bez akceptacji Gate).

**Słabości, które blokują dalszy rozwój:**
1. **Mało prawdziwych dowodów.** Benchmark P2.5 to kalibrowana symulacja; realne są pojedyncze przebiegi (Level C n=1, 2 runy autonomii V0.3, kilkanaście przypadków planera). Nic nie zbiera danych produkcyjnych: `VERIFIED_PRODUCTION_RUNS.jsonl` nie ma zapisu w kodzie, `POLICY_HEALTH.json` nie istnieje. Warunek „100 zweryfikowanych runów” dla trybu kontrolowanego jest dziś nieosiągalny z definicji.
2. **Trzy systemy telemetrii i dwa benchmarki.** `execution_events.jsonl`/analityka, `shadow_diff`/`POLICY_HEALTH` (adaptive_policy) i `telemetry.jsonl`/`aaw_experience` (PR #6); ceny w kodzie (`empirical_benchmark`) i w `MODEL_PRICING.json`. Benchmark symulowany (strategie S0–S3) i benchmark z prawdziwych runów (koszt na rozwiązane zadanie) odpowiadają na różne pytania, ale nazywają się podobnie.
3. **Dwie ścieżki wykonania.** Iteracje (szeregowe) i workflow DAG (`workflow_runner`, równoległość 2–4) — z duplikacją wywołań CLI (`autonomy_adapters.py:429` kopiuje kształt `execute_llm_node`) i rozjechanymi sufitami równoległości (3 vs 4). Gałąź adaptacyjna DAG w kontrolerze jest martwa (tryby zablokowane na stałe).
4. **Rozjazd dokumentacji z kodem.** Raporty deklarują „APPROVED FOR CONTROLLED PRODUCTION” dla trybu, który kod zawsze odrzuca; wersje 0.2.0 / 0.4.0 / „MVP V0.2” / `AAW/0.2`; `QUICK_START.md` opisuje stary 7-krokowy kreator; liczby testów w raportach od 225 do 930.
5. **UI nie pokazuje tego, co silnik już umie.** Brak w Studio: weryfikacji modeli i ponownej detekcji CLI, widoku na żywo łańcucha i prognozy, doświadczenia/benchmarku, rozpisu pomysłu, rekomendacji modelu, stanu trybu adaptacyjnego (SHADOW). Martwe ustawienie adaptive w settings, nieużywany stary bundle w paczce.

**Wniosek strategiczny:** największą dźwignią nie jest teraz kolejny mechanizm autonomii, tylko **zamknięcie pętli dowodowej**: każdy prawdziwy run → jeden rekord telemetrii → decyzja człowieka na Gate jako etykieta prawdy → benchmark i rekomendacje → (dopiero później) odblokowanie adaptacji. Zadania z raportu przekazania (podpis kryptograficzny, canary) są słuszne, ale **przedwczesne bez danych** — ustawiam je za fazą dowodową, nie przed nią.

---

## 2. Zasady roadmapy (niezmienniki, które respektuje każdy etap)

- Human Gate jest ostateczny; ACCEPT daje tylko `READY_FOR_EXTERNAL_INTEGRATION`; brak auto-merge/push/PR; żadne narzędzie MCP nie akceptuje Gate.
- Jeden silnik autonomii — rozwijamy `autonomy_controller` + `workflow_runner`, bez drugiego schedulera ani drugiego systemu ewidencji.
- SHADOW pozostaje pasywny; tryby `CONTROLLED_ADAPTIVE` / `ACTIVE` zablokowane, dopóki etap 4 nie dostarczy podpisanej autoryzacji i danych.
- Fail-closed, merge transakcyjny, zakres ścieżek ograniczony do worktree.
- Runtime Pythona bez zależności zewnętrznych (Node tylko w buildzie Studio). Każdy wyjątek (np. biblioteka kryptograficzna) wymaga osobnej decyzji.
- Brak cichej podmiany modelu i fallbacku do płatnego API.
- Uczciwość liczb: każdy wynik ma proweniencję (`REAL_MEASURED` / `ESTIMATED` / `SIMULATED`), progi n i zakresy zamiast obietnic.
- Każda zmiana przez gałąź, PR i zielone CI (Linux + Windows); live provider / wydatki tylko za zgodą użytkownika.

---

## 3. Etapy

### Etap 0 — Konsolidacja i prawda o stanie (1–2 tyg.) · *warunek wszystkiego dalej*

| # | Zadanie | Kryterium ukończenia |
|---|---|---|
| 0.1 | Scalić PR #6 do `main` (chain mode, telemetria, doświadczenie, eksploracja, rozpis pomysłu) | zielone CI na `main` |
| 0.2 | **Jeden rekord telemetrii**: `AAW_TELEMETRY_V1` jako jedyny zapis per wywołanie; `shadow_diff` i rekomendacja `adaptive_policy` dopisywane jako pola tego rekordu; analityka (`execution_events`) czyta z niego; jedno źródło cen `MODEL_PRICING.json` (usunąć ceny z kodu `empirical_benchmark`) | test spójności: jedno wywołanie → jeden rekord, wszystkie konsumenty czytają ten sam |
| 0.3 | Rozdzielić nazwy benchmarków: **„Symulator strategii”** (`empirical_benchmark`, oznaczony `SIMULATED`) vs **„Benchmark z prawdziwej pracy”** (`aaw_experience`, `BENCH/`) | żaden raport ani ekran nie miesza proweniencji |
| 0.4 | Strona statusu generowana z kodu (statusy funkcji, liczba testów z pytest, wersja z `product_version`); poprawić raporty „APPROVED” dla trybu zablokowanego; jedna wersja (0.5.0) w README / pyproject / `product_version` / `APP_VERSION` / `package.json`; `QUICK_START` pod Studio | test, który failuje przy rozjeździe wersji; brak sprzecznych statusów |
| 0.5 | Higiena paczki: `emptyOutDir: true` (lub czyszczenie `assets/`), usunięcie martwego ustawienia adaptive z settings | paczka zawiera tylko bundle z `index.html` |
| 0.6 | Wspólny wywoływacz CLI dla iteracji i workflow (usunąć duplikację `autonomy_adapters` ↔ `execute_llm_node`); jeden sufit równoległości | jedna implementacja, testy obu ścieżek przechodzą |

### Etap 1 — Pętla dowodowa z prawdziwej pracy (2–3 tyg.) · *największa dźwignia*

| # | Zadanie | Kryterium ukończenia |
|---|---|---|
| 1.1 | **Decyzja na Gate = etykieta prawdy**: ACCEPT / REJECT / kontynuacja zapisywane automatycznie do `VERIFIED_PRODUCTION_RUNS.jsonl` (z hashami mandatu, kandydata, rekordami telemetrii, wynikiem SHADOW) | każdy zamknięty run produkcyjny ma wpis; test end-to-end |
| 1.2 | Ocena SHADOW na prawdziwych danych: „co polecał tryb adaptacyjny vs co się stało” (czy rekomendacja zaoszczędziłaby czas/koszt bez spadku jakości), licznik postępu do progu odblokowania | raport/ekran z n, odsetkiem zgodności, przedziałami ufności |
| 1.3 | Benchmark z prawdziwej pracy jako domyślne źródło rekomendacji modeli (per rodzaj zadania, per użytkownik), z progami n=3/8 i oszczędnością ≥10% | rekomendacja zawsze z n i przedziałem; nigdy z danych symulowanych |
| 1.4 | Pierwszy **prawdziwy przebieg `BENCH/`** (9 zadań, ukryte testy) na 2–3 profilach — **tylko za zgodą i budżetem użytkownika** | raport `REAL_MEASURED`, koszt na rozwiązane zadanie per profil |
| 1.5 | Weryfikacja live brakujących ról: `agy` (Gemini), final review Opus/Sonnet 5.5, `review_prep` | status każdego profilu z dowodem, nie z deklaracji |

### Etap 2 — Od pomysłu do programu w jednym ciągu (3–4 tyg.) · *wartość dla użytkownika*

| # | Zadanie | Kryterium ukończenia |
|---|---|---|
| 2.1 | Rozpis pomysłu w Studio: luźny pomysł → zakres, roadmapa, ryzyka, horyzont i koszt przed START (backend `/api/intake/propose` gotowy) | użytkownik startuje run z rozpisu bez ręcznego przepisywania |
| 2.2 | Edytowalna roadmapa przed startem i **między łańcuchami** (dodaj/usuń/zmień kolejność punktów, oznacz `[człowiek]`); kontynuacja bez utraty kontekstu | nowa wersja mandatu jako nowy run, z jawnym diffem roadmapy |
| 2.3 | Widok na żywo łańcucha: sloty serii, licznik kosztu, prognoza końca, odłożone drobne uwagi, rozliczenie na Gate (backend `live`/`settlement` gotowy) | widoczne w Command Center w trakcie runu |
| 2.4 | Rekomendacja modelu potwierdzana przez użytkownika (przycisk „użyj rekomendowanego”) i kontrolowana eksploracja (opt-in) w ustawieniach | zmiana zawsze audytowana w rekordzie zadania |
| 2.5 | Szablony projektów (CLI, web API, biblioteka, aplikacja z UI) z dobrymi domyślnymi kryteriami akceptacji i dowodami | start z szablonu w ≤3 kliknięciach |
| 2.6 | Weryfikacja modeli i ponowna detekcja CLI w Studio (backend `/api/models/verify`, `/api/providers/detect` gotowy) | brak „martwych” profili na liście wyboru |

### Etap 3 — Wiele zadań i projektów (3 tyg.)

| # | Zadanie | Kryterium ukończenia |
|---|---|---|
| 3.1 | Kolejka zadań: wiele runów w różnych repozytoriach, priorytety, limit równoczesnych runów zależny od kwot dostawców | kolejka przeżywa restart aplikacji |
| 3.2 | Portfel projektów: przegląd wszystkich projektów, ich roadmap i kosztów | jeden ekran „co się dzieje we wszystkich projektach” |
| 3.3 | Budżety: limit kosztu/czasu per run i per tydzień, z bezpiecznym zatrzymaniem na granicy etapu | przekroczenie = PAUSE z wyjaśnieniem, nigdy kill w EXECUTE |
| 3.4 | Powiadomienia (Gate czeka, run wstrzymany, budżet bliski) — systemowe Windows + opcjonalnie MCP | użytkownik nie musi patrzeć w okno |

### Etap 4 — Kontrolowana adaptacja (po spełnieniu warunków danych) (3–4 tyg.)

Warunek wejścia: etap 1 dostarczył ≥100 zweryfikowanych runów z oceną SHADOW i rekomendacja SHADOW nie pogarsza jakości w przedziale ufności.

| # | Zadanie | Kryterium ukończenia |
|---|---|---|
| 4.1 | **Podpisana autoryzacja** trybu kontrolowanego: token z identyfikatorem operatora, hashem polityki, `expires_at`, budżetem i zakresem zadań; brak/niezgodność podpisu = fail-closed do SHADOW. Decyzja do podjęcia: Ed25519 wymaga biblioteki spoza stdlib (łamie zasadę „bez zależności”) — alternatywa: weryfikacja przez narzędzie systemowe lub minimalna implementacja z wektorami testowymi | test: zły podpis, wygasły token, inny hash polityki → SHADOW |
| 4.2 | Canary: maks. 2 agenty, tylko zadania `complexity ≤ LOW`, pliki krytyczne zawsze pojedynczo, circuit breaker do DISABLED po 1 awarii merge lub 2 błędach wykonawcy | breaker przetestowany wstrzykiwaniem awarii |
| 4.3 | Równoległe kroki work packetu przez workflow DAG (fan-out niezależnych kroków w osobnych worktree, transakcyjny merge) — **zbieżność dwóch ścieżek wykonania** | iteracja z 2 niezależnymi krokami kończy się szybciej przy tej samej jakości (dowód `REAL_MEASURED`) |
| 4.4 | Panel adaptacji w Studio: tryb, stan autoryzacji, postęp do progów, decyzje breakera | każda decyzja adaptacyjna ma uzasadnienie widoczne dla człowieka |

### Etap 5 — Dojrzałość produktu (ciągłe)

- Interfejs po angielsku (i18n), podpis cyfrowy `AAW.exe`, kanał aktualizacji, kanał zgłaszania podatności.
- MCP: narzędzia tylko-do-odczytu dla portfela i benchmarku; nadal brak akceptacji Gate.
- Dynamiczny planner (mutacja grafu w granicach mandatu) — dopiero po etapie 4, bo zmienia zamrożone bindingi.

---

## 4. Kolejność i zależności

```
Etap 0 ──► Etap 1 ──► Etap 4
   │          │
   └──► Etap 2 ──► Etap 3 ──► Etap 5
```

Etap 2 może iść równolegle z etapem 1 (UI na gotowym backendzie). Etap 4 jest twardo zależny od danych z etapu 1.

## 5. Widoki UI/UX wynikające z roadmapy

| Widok w Studio | Etap | Stan backendu | Uwagi |
|---|---|---|---|
| **Doświadczenie** — benchmark z prawdziwej pracy (wykres koszt × skuteczność per rodzaj zadania, rekomendacje, proweniencja) | 1.3, 0.3 | gotowy (`/api/experience`) | realizowany teraz |
| **Na żywo** w Command Center — sloty łańcucha, licznik, prognoza, odłożone uwagi; **rozliczenie** na Gate | 2.3 | gotowy (`run.live`, `gate.settlement`) | realizowany teraz |
| **Od pomysłu** w kreatorze — rozpis, horyzont, koszt, tryb łańcuchowy, rekomendacja modelu | 2.1, 2.4 | gotowy (`/api/intake/propose`, `/api/experience/forecast`) | realizowany teraz |
| **Adaptacja i dowody** w ustawieniach — tryb (SHADOW/zablokowany), postęp do progów etapu 4, eksploracja | 1.2, 4.4 | częściowy (status trybu z kodu; licznik runów po 1.1) | realizowany teraz w zakresie danych, które istnieją; reszta oznaczona jako planowana |
| Weryfikacja modeli / ponowna detekcja | 2.6 | gotowy | realizowany teraz |
| Kolejka i portfel projektów, budżety | 3.x | brak | projekt po etapie 2 |
| Panel breakera i autoryzacji | 4.x | brak | projekt po etapie 1 |

## 6. Ryzyka

- **Koszt prawdziwych przebiegów** (etap 1.4/1.5) — wymaga zgody i budżetu; bez nich etap 4 się nie odblokuje.
- **Zależność kryptograficzna** (4.1) — decyzja architektoniczna przed implementacją.
- **Rozjazd dwóch ścieżek wykonania** narasta z każdą zmianą — 0.6 nie powinno czekać.
- **Dokumentacja jako źródło fałszywej pewności** — 0.4 przed jakimkolwiek kolejnym raportem „APPROVED”.
