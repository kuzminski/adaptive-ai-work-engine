# Example 2 — Tests and input validation for an existing project

For a project you already have. AAW first pins down current behaviour with
tests, then improves error handling — without changing your own folder.

**Goal (Cel)**

> Zwiększ niezawodność istniejącego kodu: testy dla obecnego zachowania i czytelne błędy przy złych danych wejściowych.
> *(Make the existing code more reliable: tests for current behaviour and clear errors for bad input.)*

**First iteration (Pierwsza iteracja)**

> Dodaj testy jednostkowe opisujące obecne zachowanie głównego modułu (bez zmiany logiki).

**Direction (Kierunek)**

```
- walidacja danych wejściowych z czytelnymi komunikatami
- krótka dokumentacja w README
```

Tip: under „Zaawansowane" on the START step you can list folders AAW must not
touch (Obszary zabronione), e.g. `migrations/`.
