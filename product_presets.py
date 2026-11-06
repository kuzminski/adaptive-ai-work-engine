#!/usr/bin/env python3
"""AAW — example project presets for the New Task wizard.

A preset is only an EXAMPLE of how to fill the three free-text fields (goal,
first iteration, direction/roadmap). Loading one copies its text into the form;
the user edits it freely and nothing about a preset is mandatory or validated
differently from hand-typed text. Presets are plain data so they ship in the
portable build and cloud/local agents can extend them in one place.
"""

from __future__ import annotations

from typing import Any

PRESETS: list[dict[str, Any]] = [
    {
        "id": "autonomous_mvp",
        "title": "Autonomiczne MVP (wzorzec długiej pracy)",
        "description": "Pokazuje, jak opisać cel, małą pierwszą iterację i szeroki kierunek, aby AAW pracował "
                       "wiele iteracji bez operatora.",
        "goal": (
            "Doprowadź aplikację autonomicznie do kompletnego, stabilnego i użytecznego MVP.\n"
            "Pracuj iteracyjnie: PLAN → IMPLEMENT → VERIFY → REVIEW → REPAIR → NEXT PLAN.\n"
            "Nie czekaj na człowieka pomiędzy poprawnie zakończonymi iteracjami. Kontynuuj do wyczerpania "
            "sensownej roadmapy lub wystąpienia rzeczywistej blokady wymagającej człowieka."
        ),
        "first_iteration": (
            "Zbadaj repozytorium, dokumentację, testy, UI, model danych i istniejącą implementację.\n"
            "Zidentyfikuj najważniejszą lukę blokującą użyteczne MVP. Wybierz małą, zamkniętą iterację o "
            "najwyższej wartości, zaimplementuj ją, przetestuj, wykonaj review i napraw problemy."
        ),
        "directions": (
            "- Stabilny runtime: obsługa błędów, wznawianie, czytelne komunikaty\n"
            "- Brakujące funkcje potrzebne do użytecznego MVP, od najważniejszych\n"
            "- Integracja komponentów i testy regresyjne krytycznych ścieżek\n"
            "- UX: puste stany, czytelność, spójność kontrolek\n"
            "- Dokumentacja użytkownika i krótki opis architektury\n"
            "Roadmapa nie jest zamkniętą listą zadań: po każdej iteracji oceń rzeczywisty stan produktu i "
            "wybierz następną najważniejszą pracę."
        ),
    },
    {
        "id": "expense_tracker",
        "title": "Tracker wydatków (Python)",
        "description": "Mały projekt od zera: model danych, CLI, eksport.",
        "goal": "Prosta aplikacja w Pythonie do śledzenia wydatków: dodawanie wydatków, lista i eksport do CSV.",
        "first_iteration": "Model danych wydatku (kwota, kategoria, data, opis) i zapis/odczyt z pliku JSON, z testami jednostkowymi.",
        "directions": "- polecenia w terminalu: dodaj, lista, usuń\n- eksport do CSV\n- podsumowanie miesięczne",
    },
    {
        "id": "tests_and_validation",
        "title": "Testy i walidacja w istniejącym projekcie",
        "description": "Utwardzanie istniejącego kodu bez zmiany logiki.",
        "goal": "Zwiększ niezawodność istniejącego kodu: testy dla obecnego zachowania i czytelne błędy przy złych danych wejściowych.",
        "first_iteration": "Dodaj testy jednostkowe opisujące obecne zachowanie głównego modułu (bez zmiany logiki).",
        "directions": "- walidacja danych wejściowych z czytelnymi komunikatami\n- krótka dokumentacja w README",
    },
    {
        "id": "todo_web_page",
        "title": "Lista zadań w przeglądarce (HTML/JS)",
        "description": "Mała strona bez serwera.",
        "goal": "Mała strona HTML/JavaScript z listą zadań do zrobienia, działająca bez serwera.",
        "first_iteration": "Strona index.html z dodawaniem i usuwaniem zadań zapisywanych w localStorage.",
        "directions": "- filtrowanie: wszystkie / zrobione / do zrobienia\n- prosty, czytelny wygląd\n- eksport listy do pliku JSON",
    },
]


def list_presets() -> list[dict[str, Any]]:
    return [dict(p) for p in PRESETS]


def get_preset(preset_id: str) -> dict[str, Any] | None:
    return next((dict(p) for p in PRESETS if p["id"] == preset_id), None)


# --- Project templates (roadmap 2.5) -------------------------------------------------------------------------------
# A template is a ready-made, editable starting point for a KIND of project: goal, a small first iteration, a roadmap
# and sensible acceptance criteria / evidence / forbidden areas. Like presets it is plain data copied into the wizard;
# nothing in it is validated differently from hand-typed text and the user may change every field before START.

PROJECT_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "tpl_cli",
        "kind": "cli",
        "title": "Narzędzie CLI",
        "description": "Program uruchamiany w terminalu: polecenia, argumenty, kody wyjścia.",
        "goal": "Działające narzędzie wiersza poleceń z czytelnym --help, jednoznacznymi kodami wyjścia i testami.",
        "first_iteration": "Szkielet CLI (parser argumentów, jedno działające polecenie) z testami uruchamiającymi program jako proces.",
        "directions": "- kolejne polecenia, od najważniejszych\n- czytelne komunikaty błędów i kody wyjścia\n- plik konfiguracji lub zmienne środowiskowe\n- README z przykładami użycia",
        "advanced": {
            "acceptance_criteria": "`--help` opisuje każde polecenie\nzły argument kończy się niezerowym kodem wyjścia i komunikatem na stderr\nkażde polecenie ma test uruchamiający program jako proces",
            "required_evidence": "testy jednostkowe\ntest uruchomienia procesu CLI",
            "forbidden_areas": "",
            "risks": [],
        },
    },
    {
        "id": "tpl_web_api",
        "kind": "web_api",
        "title": "API webowe (HTTP)",
        "description": "Usługa HTTP z punktami końcowymi, walidacją wejścia i testami kontraktu.",
        "goal": "Usługa HTTP z udokumentowanymi punktami końcowymi, walidacją danych wejściowych i spójnym formatem błędów.",
        "first_iteration": "Jeden punkt końcowy zdrowia (/health) i jeden zasób z odczytem i zapisem, z testami żądań HTTP.",
        "directions": "- pozostałe operacje na zasobach (CRUD)\n- walidacja wejścia i spójne kody statusu\n- trwałe przechowywanie danych\n- uwierzytelnianie, jeśli wymagane\n- opis API (README lub OpenAPI)",
        "advanced": {
            "acceptance_criteria": "każdy punkt końcowy ma test żądania i odpowiedzi\nbłędne dane wejściowe zwracają 4xx z opisem błędu\n/health zwraca 200",
            "required_evidence": "testy jednostkowe\ntesty integracyjne żądań HTTP",
            "forbidden_areas": "pliki z sekretami i konfiguracją produkcyjną",
            "risks": [{"description": "Uwierzytelnianie i dane użytkowników wymagają przeglądu przez człowieka.",
                       "severity": "MEDIUM", "item_ids": [], "source": "TEMPLATE"}],
        },
    },
    {
        "id": "tpl_library",
        "kind": "library",
        "title": "Biblioteka",
        "description": "Moduł do użycia w innych projektach: stabilne API publiczne i testy.",
        "goal": "Biblioteka ze stabilnym, opisanym API publicznym, pełnym zestawem testów i przykładami użycia.",
        "first_iteration": "Zdefiniuj API publiczne (sygnatury i dokumentacja) oraz zaimplementuj jedną funkcję z testami jednostkowymi.",
        "directions": "- pozostałe funkcje API publicznego\n- przypadki brzegowe i obsługa błędów\n- przykłady użycia w README\n- pakowanie i numer wersji",
        "advanced": {
            "acceptance_criteria": "każda funkcja publiczna ma dokumentację i test\nprzypadki brzegowe są pokryte testami\nAPI publiczne nie zmienia się bez zmiany wersji",
            "required_evidence": "testy jednostkowe",
            "forbidden_areas": "",
            "risks": [],
        },
    },
    {
        "id": "tpl_ui_app",
        "kind": "ui_app",
        "title": "Aplikacja z interfejsem",
        "description": "Aplikacja z ekranami: stany puste, błędy, dostępność i test w przeglądarce.",
        "goal": "Aplikacja z działającym interfejsem: ekrany główne, czytelne stany puste i błędów oraz podstawowa dostępność.",
        "first_iteration": "Jeden działający ekran główny z danymi przykładowymi i testem, który otwiera go i sprawdza kluczowe elementy.",
        "directions": "- pozostałe ekrany i nawigacja\n- stany puste, ładowania i błędów\n- dostępność (etykiety, fokus, kontrast)\n- układ na wąskim ekranie\n- krótka instrukcja użytkownika",
        "advanced": {
            "acceptance_criteria": "każdy ekran ma stan pusty i stan błędu\nkluczowe ścieżki użytkownika mają test w przeglądarce lub test komponentu\nukład nie psuje się na wąskim ekranie",
            "required_evidence": "testy komponentów lub test w przeglądarce\nbuild bez błędów",
            "forbidden_areas": "",
            "risks": [],
        },
    },
]


def list_templates() -> list[dict[str, Any]]:
    return [dict(t) for t in PROJECT_TEMPLATES]


def get_template(template_id: str) -> dict[str, Any] | None:
    return next((dict(t) for t in PROJECT_TEMPLATES if t["id"] == template_id), None)
