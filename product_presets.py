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
