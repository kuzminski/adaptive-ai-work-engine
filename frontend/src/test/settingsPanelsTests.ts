/**
 * AAW Agentic Studio — Settings Panels Component Tests
 *
 * Validates rendering of AdaptationPanel (adaptive mode status, SHADOW recommendation,
 * controlled exploration, planned items) and ModelVerifyPanel (provider detection,
 * model states, empty states) via renderToString.
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { AdaptationPanel } from "../components/settings/AdaptationPanel";
import { ModelVerifyPanel } from "../components/settings/ModelVerifyPanel";
import { engineStore } from "../store/engineStore";

export async function runSettingsPanelsTests() {
  console.log("\n=== Starting AAW Settings Panels Tests ===");

  // ── Test A: AdaptationPanel honest status and empty states ───────────────
  console.log("\n[Test A1] AdaptationPanel: mode facts, progress placeholder, empty recommendation");
  {
    engineStore.actions.reset();
    const html = renderToString(React.createElement(AdaptationPanel));

    assert(html.includes("data-testid=\"adaptation-panel\""), "Must render adaptation panel container");
    assert(html.includes("Tryb adaptacyjny"), "Must render adaptive mode section heading");
    assert(html.includes("data-testid=\"mode-shadow\""), "Must render SHADOW mode row");
    assert(html.includes("Aktywny (domyślny)"), "SHADOW must be shown as the active default");
    assert(html.includes("data-testid=\"mode-controlled-adaptive\""), "Must render CONTROLLED_ADAPTIVE row");
    assert(html.includes("Zablokowany"), "CONTROLLED_ADAPTIVE must be shown as locked");
    assert(
      html.includes("CONTROLLED_ADAPTIVE_LOCKED_PENDING_TRUSTED_AUTHORIZATION"),
      "Must show the exact refusal code for controlled mode"
    );
    assert(html.includes("minimum 100 zweryfikowanych"), "Must show the verified-runs threshold (100)");
    assert(html.includes("data-testid=\"mode-active\""), "Must render ACTIVE mode row");
    assert(html.includes("UNAUTHORIZED_ACTIVE_MODE"), "Must show the ACTIVE refusal code");
    assert(html.includes("Zablokowany na stałe"), "ACTIVE must be shown as permanently locked");
    assert(
      html.includes("licznik pojawi się po wdrożeniu etapu 1.1"),
      "Must show the progress placeholder instead of an invented number"
    );
    assert(html.includes("data-testid=\"shadow-no-run\""), "Without a run, must show the no-run empty state");

    console.log("  ✓ Adaptive mode facts, locked states and progress placeholder verified");
  }

  console.log("\n[Test A2] AdaptationPanel: exploration opt-in controls and planned section");
  {
    engineStore.actions.reset();
    const html = renderToString(React.createElement(AdaptationPanel));

    assert(html.includes("data-testid=\"exploration-section\""), "Must render exploration section");
    assert(html.includes("Kontrolowana eksploracja"), "Must render exploration heading");
    assert(html.includes("data-testid=\"exploration-toggle\""), "Must render exploration toggle");
    assert(html.includes("role=\"switch\""), "Exploration toggle must expose switch semantics");
    assert(html.includes("data-testid=\"exploration-max-percent\""), "Must render max percent input");
    assert(html.includes("data-testid=\"exploration-max-per-run\""), "Must render max per run input");
    assert(html.includes("data-testid=\"exploration-save-btn\""), "Must render exploration save button");
    assert(html.includes("data-testid=\"planned-section\""), "Must render planned section");
    assert(html.includes("Jeszcze niedostępne"), "Planned section must be marked as not yet available");
    assert(html.includes("Circuit breaker"), "Planned section must list the circuit breaker");
    assert(html.includes("Canary"), "Planned section must list the canary rollout");

    console.log("  ✓ Exploration controls and planned items verified");
  }

  console.log("\n[Test A3] AdaptationPanel: SHADOW recommendation for the loaded run");
  {
    engineStore.actions.reset();
    engineStore.actions._setState({
      run: {
        run_id: "run-adaptive-1",
        status: "RUNNING",
        adaptive_orchestration: {
          enabled: true,
          selected_template: "PARALLEL_WORKERS",
          max_parallel_workers: 3,
          serialization_reason: "SCOPE_OVERLAP_SERIALIZATION_REQUIRED",
        },
      } as any,
    });
    const html = renderToString(React.createElement(AdaptationPanel));

    assert(html.includes("data-testid=\"shadow-recommendation\""), "Must render recommendation block");
    assert(html.includes("PARALLEL_WORKERS"), "Must show the selected template");
    assert(html.includes(">3<"), "Must show the worker count");
    assert(html.includes("SCOPE_OVERLAP_SERIALIZATION_REQUIRED"), "Must show the serialization reason");
    assert(html.includes("pasywna, nie zmieniła wykonania"), "Must state the recommendation is passive");
    assert(!html.includes("data-testid=\"shadow-no-data\""), "Must not show the no-data state when data exists");

    engineStore.actions._setState({
      run: { run_id: "run-plain-1", status: "RUNNING" } as any,
    });
    const htmlNoAdaptive = renderToString(React.createElement(AdaptationPanel));
    assert(
      htmlNoAdaptive.includes("Brak danych adaptacyjnych dla przebiegu run-plain-1"),
      "Run without adaptive data must show an honest empty state"
    );
    assert(!htmlNoAdaptive.includes("PARALLEL_WORKERS"), "Must not leak another run's template");

    console.log("  ✓ SHADOW recommendation and no-data state verified");
  }

  // ── Test B: ModelVerifyPanel detection, states and empty states ──────────
  console.log("\n[Test B1] ModelVerifyPanel: buttons and empty state without detection");
  {
    engineStore.actions.reset();
    const html = renderToString(React.createElement(ModelVerifyPanel));

    assert(html.includes("data-testid=\"model-verify-panel\""), "Must render model verify panel");
    assert(html.includes("data-testid=\"detect-providers-btn\""), "Must render detect button");
    assert(html.includes("Wykryj ponownie narzędzia AI"), "Detect button must have its Polish label");
    assert(html.includes("data-testid=\"verify-models-btn\""), "Must render verify button");
    assert(html.includes("Sprawdź modele"), "Verify button must have its Polish label");
    assert(html.includes("data-testid=\"provider-empty\""), "Without detection must show the provider empty state");
    assert(!html.includes("data-testid=\"verify-results\""), "No verification results before a run");
    assert(!html.includes("data-testid=\"model-verify-error\""), "No error block before any action");

    console.log("  ✓ Buttons and empty state verified");
  }

  console.log("\n[Test B2] ModelVerifyPanel: provider and model states from bootstrap detection");
  {
    engineStore.actions.reset();
    engineStore.actions._setState({
      bootstrap: {
        version: "test",
        providers: {
          detected_at: "2026-11-03T10:00:00+01:00",
          usable_profile_count: 2,
          providers: [
            {
              provider_id: "claude",
              display_name: "Claude CLI",
              harness: "claude",
              status: "FOUND",
              version: "2.1.0",
              login: "LOGGED_IN",
              login_detail: null,
              models: [
                { runtime_model_id: "claude-sonnet-5-5", state: "AVAILABLE", label: "dostępny" },
                { runtime_model_id: "claude-haiku-5-5", state: "NEEDS_CHECK", label: "wymaga sprawdzenia przed użyciem" },
              ],
            },
            {
              provider_id: "codex",
              display_name: "Codex CLI",
              harness: "codex",
              status: "NOT_FOUND",
              version: null,
              login: "UNKNOWN",
              login_detail: "CLI nie jest zainstalowane lub nie ma go w PATH",
              models: [{ runtime_model_id: "gpt-5-codex", state: "CLI_NOT_FOUND", label: "brak CLI" }],
            },
            {
              provider_id: "antigravity",
              display_name: "Antigravity CLI",
              harness: "antigravity",
              status: "FOUND",
              version: "0.9.1",
              login: "NOT_LOGGED_IN",
              login_detail: null,
              models: [{ runtime_model_id: "gemini-3.8-flash", state: "NOT_LOGGED_IN", label: "CLI niezalogowane" }],
            },
          ],
        },
      } as any,
    });

    const html = renderToString(React.createElement(ModelVerifyPanel));

    assert(html.includes("data-testid=\"provider-list\""), "Must render provider list");
    assert(!html.includes("data-testid=\"provider-empty\""), "Must not show empty state when providers exist");
    assert(html.includes("Claude CLI"), "Must show provider display name");
    assert(html.includes("wersja: 2.1.0"), "Must show provider version");
    assert(html.includes("logowanie: zalogowane"), "Must show logged-in state");
    assert(html.includes("data-testid=\"provider-status-codex\""), "Must render status for missing CLI");
    assert(html.includes("nie wykryto"), "Missing CLI must be shown as not found");
    assert(html.includes("wersja: —"), "Missing version must be shown as a dash, not invented");
    assert(html.includes("CLI nie jest zainstalowane lub nie ma go w PATH"), "Must show backend login detail");
    assert(html.includes("claude-sonnet-5-5"), "Must show model ID");
    assert(html.includes("dostępny"), "Must show runnable model label from backend");
    assert(html.includes("wymaga sprawdzenia przed użyciem"), "Must show needs-check model label from backend");
    assert(html.includes("brak CLI"), "Must show CLI-missing model label from backend");
    assert(html.includes("CLI niezalogowane"), "Must show logged-out model label from backend");
    assert(html.includes("Profile gotowe do użycia"), "Must show usable profile count");
    assert(html.includes(">2<"), "Must show usable profile count value");

    console.log("  ✓ Provider detection, versions, login and model labels verified");
  }

  console.log("\n[Test B3] ModelVerifyPanel: provider with no models shows honest empty line");
  {
    engineStore.actions.reset();
    engineStore.actions._setState({
      bootstrap: {
        version: "test",
        providers: {
          providers: [
            {
              provider_id: "claude",
              display_name: "Claude CLI",
              harness: "claude",
              status: "FOUND",
              version: null,
              login: "UNKNOWN",
              models: [],
            },
          ],
        },
      } as any,
    });
    const html = renderToString(React.createElement(ModelVerifyPanel));
    assert(html.includes("Brak modeli przypisanych do tego narzędzia."), "Provider without models must say so");
    assert(!html.includes("data-testid=\"usable-profile-count\""), "No usable count when backend did not send it");

    console.log("  ✓ Provider without models verified");
  }

  console.log("\n=== ALL SETTINGS PANELS TESTS PASSED ===");
}
