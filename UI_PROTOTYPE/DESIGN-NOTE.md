# AAW Canvas - nota projektowa V0.1

> **Status od AAW_CANVAS_FUNCTIONALIZATION_V0.1.**
>
> Symulowany canvas zostal **zarchiwizowany** jako
> `DESIGN_REFERENCE/aaw-canvas.simulated.html` - poza katalogiem `UI_PROTOTYPE/`,
> ktory jest jedynym katalogiem serwowanym przez bridge, wiec `aaw_bridge_server`
> nie moze go podac. Zawiera `plannedVerdict`, `engine()` i `spawnRepair()`;
> jest **wylacznie referencja projektowa**, nigdy autorytetem wykonania.
> Pilnuje tego `test_the_bridge_cannot_serve_the_archived_simulation`.
>
> Uruchamialny canvas to **`UI_PROTOTYPE/aaw-canvas-live.html`**. Kazdy stan
> wezla pochodzi z `NODE_STARTED`/`NODE_COMPLETED`, werdykt z
> `NODE_COMPLETED.verdict`, stan krawedzi z `EDGE_SELECTED`/`EDGE_HELD`, galaz
> naprawcza z `BRANCH_CREATED`, a odziedziczony upstream z `RUN_RESUMED` -
> wszystko przez `aaw_bridge`.
>
>     python ux_slice_evidence.py --serve --sandbox-workflows --node-delay 0.35
>
> **Decyzja 7 (tworzenie wezlow gestem) jest juz zrealizowana**: paleta, dwuklik
> na pustym polu i przeciagniecie portu w pustke tworza wezel; przeciagniecie
> portu na inny wezel tworzy krawedz. Odroczony pozostaje wylacznie planner
> (decyzja 8) - patrz `AAW_CANVAS_FUNCTIONALIZATION_V0_1.md` sekcja 9.

Prototyp, z ktorego wywiedziono ta gramatyke wizualna: `DESIGN_REFERENCE/aaw-canvas.simulated.html` (archiwum, nieuruchamialne).

## 0. Analiza UX — co wyciągnięto z benchmarków

| Narzędzie | Zasada, którą wzięto | Czego świadomie NIE wzięto |
|---|---|---|
| Figma / FigJam | Canvas jest aplikacją. Chrome to cienki pasek + nakładki na żądanie. Selekcja → inspector, nie odwrotnie. | Stałych paneli warstw/assetów. |
| Linear | Jeden akcent na ekran, reszta neutralna. Stan czytany z formy (pasek, kropka), nie z wielkości karty. | Widoków listowych jako głównego modelu. |
| Unreal Blueprint | Nazwane porty jako kontrakt węzła. Drag z portu w pustkę = tworzy węzeł. Semantic zoom. | Gęstości pinów i typów danych. |
| Blender / Resolve | Wysoka funkcjonalność w zwartej, cichej powierzchni; skróty klawiszowe jako główny akcelerator. | Wielookienności i modalnych trybów. |
| n8n / Node-RED | **Tylko jako opis problemu**: gdy runtime i topologia mieszają się w jednym widoku, użytkownik czyta logi zamiast grafu. | Stylistyki, palety, dużych kolorowych kart. |

Wniosek prowadzący cały projekt: **wynik zadania musi mieszkać na grafie, nie w logu**. Log jest dowodem, graf jest interfejsem.

## 1. Osiem decyzji UX

**1. Canvas ≈ 88% powierzchni; zero stałych paneli.**
Jedyny trwały chrome to 46 px pasek przyrządowy. Inspector, hover-peek, pasek plannera i pigułki zoomu to nakładki — canvas pod nimi zachowuje pełną szerokość i nigdy się nie kurczy. *Dlaczego:* każdy stały panel to stała opłata za rzecz potrzebną przez 5% czasu.

**2. Node pokazuje cztery rzeczy, nigdy pięć.**
`ID` · nazwa · rola · stan, plus dwuliniowy opis. Model, effort, tokeny, czas, `runtime_model_id`, `independence` są w inspectorze pod zwiniętym `Binding & telemetry`. *Dlaczego:* jeśli metadane są na node'cie, graf czyta się jak tabela procesów, a nie jak plan pracy.

**3. Werdykt review renderuje się inline, przyklejony do node'a, który go wydał.**
Karta z `REPAIR`, streszczeniem i pierwszą linią `next_brief` pojawia się pod N04 w momencie decyzji i zostaje, dopóki jej nie odrzucisz. *Dlaczego:* to jest ta jedna informacja, dla której użytkownik normalnie otwierałby log. Postawienie jej na canvasie usuwa cały panel Activity Log.

**4. REPAIR to rodzeństwo na grafie, nie ukryty retry.**
Reviewer nie zawraca do tego samego taska. Powstaje jawny `N04A` z briefem autorstwa reviewera, połączony nazwanym portem `REPAIR`. Gałąź PASS nie znika — dostaje stan `HELD` (przerywana, wygaszona krawędź z etykietą `PASS · HELD`). *Dlaczego:* `max_repair_cycles = 2` z V0.2 jest niewidoczne w UI i nieodróżnialne od zawieszenia. Rozgałęzienie jest samo-dokumentujące, a stara ścieżka zostaje w historii.

**5. Hierarchia stanu przez formę, nie przez rozmiar.**
Wszystkie node'y mają identyczne wymiary. Różnicują je: 2 px pasek semantyczny, kropka, waga tytułu i krycie. `running` = pełny kontrast + poświata; `done` = 60% krycia, wraca do 95% na hover; `pending` = neutralny; `repair`/`blocked` = ostrzegawczy pasek i ramka. *Dlaczego:* skalowanie kart niszczy layout grafu przy każdej zmianie stanu.

**6. BUILD i RUN dzielą jeden layout, różnią się afordancjami.**
Ta sama topologia, te same pozycje. W BUILD: przeciąganie, porty, `+`/`×` na krawędziach, inspector jako edytor. W RUN: topologia zamrożona, porty nieaktywne, inspector czyta wynik (`verdict`, `summary`, `next_brief`, `carry_forward`, `artifacts`). *Dlaczego:* jeden model przestrzenny; przełączenie trybu nie każe uczyć się ekranu od nowa.

**7. Trzy sposoby stworzenia node'a, żaden przez sidebar.**
Dwuklik w pustkę · przeciągnięcie portu w pustkę (tworzy połączony node z aktywną edycją nazwy) · upuszczenie node'a na krawędź (wstawia między dwa zadania i przepina przewód). *Dlaczego:* kryterium 4 node'y < 60 s jest osiągalne tylko wtedy, gdy tworzenie i łączenie to jeden gest.

**8. Planner jest narzędziem kontekstowym, nie etapem workflow.**
`Plan from here` na zaznaczeniu → półprzezroczysty ghost subgraph z paskiem `Accept / Modify / Discard` przy propozycji. Do momentu Accept graf jest niezmieniony. Pasek podaje koszt i przesłankę: „3 steps · from N08 contract · main_merge_allowed = false · est. 2 LLM calls". *Dlaczego:* planner jest droższy niż task; musi wyglądać na decyzję, nie na automat.

## 2. Świadomie ukryte

| Ukryte | Gdzie żyje | Powód |
|---|---|---|
| `runtime_model_id`, `effort`, profil (`SOL_MEDIUM`, `TERRA_HIGH`) | inspector → zwinięte `Binding & telemetry` | Ustawiane raz na workflow, nie na node. Zamrażane w `workflow_bindings.json` przed pierwszym LLM node — po starcie i tak nieedytowalne. |
| Zużycie tokenów, czas trwania | ten sam zwinięty blok | Dane rozliczeniowe, nie operacyjne. Nie zmieniają następnej decyzji. |
| Pełny `brief` i pełny wynik | hover-peek (3 linie) → inspector (całość) | Progresywne ujawnianie zamiast ściany tekstu na canvasie. |
| Activity Log / stream zdarzeń | jedna linia w pasku + karta werdyktu na canvasie | Log jest artefaktem dowodowym (`03_STATS/<RUN_ID>/WORKFLOW/`), nie interfejsem. |
| `carry_forward`, `artifacts` | inspector, poniżej `next_brief` | Ważne przy audycie, nie przy prowadzeniu przebiegu. |
| Limity (`max_nodes`, `max_wall_time_minutes`) | poza V0.1 UI | Ujawniać dopiero przy naruszeniu, nie prewencyjnie. |
| Minimapa | brak | Zastąpiona przez `F` (fit) i semantic zoom. Minimapa to stały panel na okazjonalny problem. |

## 3. Do V0.2

1. **Jawny merge gałęzi repair** — po zakończeniu `N04A` propozycja ghost `re-review → N05` w tym samym mechanizmie co planner. Dziś przebieg zatrzymuje się na `WAITING_FOR_HUMAN`; to poprawne, ale niedokończone.
2. **Marquee select + grupowe operacje** — dziś tylko `shift`+klik. Potrzebne przy duplikowaniu większych fragmentów gałęzi.
3. **Undo / redo** — brak w V0.1. Bez tego edycja grafu jest napięta, a to najczęściej dotykana część produktu.
4. **Auto-layout gałęzi** — spawn `N04A` używa stałego offsetu `(+336, +280)`. Przy kilku równoległych repair-ach będą kolizje.
5. **Diff dwóch przebiegów tego samego workflow** — nałożenie poprzedniego `AAW_RUN_ID` na graf. Realizuje wprost obietnicę „użyj ponownie, zmieniając tylko kilka briefów".
6. **Biblioteka szablonów briefów** — dziś brief jest wolnym tekstem. Fragmenty wielokrotnego użytku bez wprowadzania edytora konfiguracji.
7. **Ostrzeżenie o wyjściu poza worktree** — pasek pokazuje `worktree · clean · canonical HEAD pinned`, ale nie reaguje na naruszenie. Powinno blokować `Run` wizualnie na canvasie.

## 4. Czego prototyp celowo nie robi

Nie projektuje: katalogu modeli, edytora limitów, ekranu Human Gate, historii przebiegów, ustawień konta, wyszukiwarki workflow, wielu zakładek. To najmniejsza wersja, która może być codziennym narzędziem — nie mapa całego AAW.
