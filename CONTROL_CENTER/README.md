# AAW Control Center V0.4

Lokalne, dependency-free centrum prowadzenia pracy AI dla Windows (Python stdlib: `tkinter` / `ttk` / `sqlite3`). Zachowuje istniejące launchery, workflow, Custom Job, kolejki, telemetrię i artefakty. Bez merge / push / API fallback / automatycznej eskalacji modelu.

V0.4 to **uproszczenie produktu** + **minimalna warstwa analityki**:

- **Dzisiaj (Home)** — jedna strona startowa: co wymaga decyzji, co w toku, co ostatnio się zakończyło, krótki status systemu.
- **Nowe zadanie** — ludzki front door: cel → *przepis* (Szybkie zadanie / Implementacja i weryfikacja / Zmiana wieloczęściowa / Zaawansowane) → Repository/Worktree → *preset wykonania* → Start. Przepisy mapują się na istniejące prymitywy (Single Task / Workflow / Custom Job); runnery nie zostały przepisane ani przemianowane.
- **Kolejki**, **Przebiegi** — bez zmian semantycznych. Przebiegi = pełne szczegóły techniczne (Summary / Timeline / Artifacts / Telemetry).
- **Wgląd (Insights)** — pochodny indeks nad `03_STATS`; presety, jakość danych, ~5 wykresów, filtry, eksport CSV.
- **Ustawienia** — trwałe preferencje (`STATE/app_settings.json`): domyślny przepis, domyślny preset, polityka preprocessingu, kolejki, modele, ścieżki, preflight.

`Workflow` i `Custom Job` nie są już osobnymi pozycjami w nawigacji — pełna konfiguracja jest pod przepisem **Zaawansowane** lub przyciskiem **Dostosuj** w Nowym zadaniu.

## Uruchomienie

Dwuklik na `START_AAW_CONTROL_CENTER.cmd` albo:

```powershell
python CONTROL_CENTER\aaw_control_center.py
```

## Presety wykonania

Kuratorowane presety nad `IMPLEMENTER_PROFILES.json` (nie ranking, nie polityka):

| Preset | IMPLEMENT | REVIEW | REPAIR | Preprocessing |
|---|---|---|---|---|
| Szybki / ograniczony | Terra / high | Sonnet / high | Terra / high | OFF |
| Zrównoważony | Sol / medium | Sonnet / high | Sol / medium | OFF |
| Dogłębny | Sol / high | Opus / high | Sol / high | OFF |
| Ze wsparciem lokalnym | Sol / medium | Sonnet / high | Sol / medium | AUTO_SAFE (Qwen) |

**Dostosuj** odsłania per-node model / effort / preprocessing na pełnej stronie Workflow / Custom Job.

## Analityka

`ANALYTICS/aaw_analytics.py` buduje `ANALYTICS/aaw_analytics.sqlite` — **wyłącznie stan pochodny**. Można go skasować i odbudować z `03_STATS`. Żaden runner nie zależy od tej bazy.

```powershell
python CONTROL_CENTER\ANALYTICS\aaw_analytics.py --rebuild
python CONTROL_CENTER\ANALYTICS\aaw_analytics.py --refresh
python CONTROL_CENTER\ANALYTICS\aaw_analytics.py --data-quality
python CONTROL_CENTER\ANALYTICS\aaw_analytics.py --self-test
```

Reguły łączenia: encje łączone **tylko** przez jawne identyfikatory (`run_id`, `node_id`, `downstream_node_id`). Brak łączenia po zbliżonych timestampach. Rekordy legacy → `link_confidence = LOW`. Referencja do nieistniejącego node → `UNLINKED`. Każdy wiersz pochodny ma `source_id` i `link_confidence`.

## Walidacja

```powershell
python -m py_compile "CONTROL_CENTER\aaw_control_center.py" "CONTROL_CENTER\ui_components.py" "CONTROL_CENTER\ui_charts.py" "CONTROL_CENTER\ANALYTICS\aaw_analytics.py"
python "CONTROL_CENTER\aaw_control_center.py" --self-test
python "CONTROL_CENTER\aaw_control_center.py" --gui-smoke-test
python "CONTROL_CENTER\aaw_control_center.py" --gui-layout-test
python "CONTROL_CENTER\aaw_control_center.py" --queue-smoke-test
python "CONTROL_CENTER\ANALYTICS\aaw_analytics.py" --self-test
python -m pytest -q "CONTROL_CENTER"
python "workflow_runner.py" --self-test
python "custom_job_runner.py" --self-test
```

`--gui-layout-test` sprawdza scroll, kółko myszy, nawigację (Home / Nowe zadanie / Kolejki / Przebiegi / Wgląd / Ustawienia) oraz dostępność przycisku START przy 1920×1080, 1366×768, 1180×760 i 980×620.
