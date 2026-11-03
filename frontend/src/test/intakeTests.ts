/**
 * AAW Agentic Studio — Idea intake and start forecast component tests.
 *
 * Covers:
 * - IdeaIntakePanel collapsed by default (no idea textarea rendered)
 * - IntakeResult rendering: scope, roadmap with human-required markers, risks, forecast, honest empty states
 * - ForecastView: empty, thin-history, recommendation with apply / undo controls
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { IdeaIntakePanel, IntakeResult, formatRange } from "../components/intake/IdeaIntakePanel";
import { ForecastView } from "../components/intake/StartForecast";

export async function runIntakeTests() {
  console.log("\n=== Starting AAW Idea Intake & Start Forecast Tests ===");

  // ── Test I1: Collapsed panel by default ───────────────────────────────────
  console.log("\n[Test I1] IdeaIntakePanel collapsed by default");
  {
    const html = renderToString(
      React.createElement(IdeaIntakePanel, { repoPath: "", chainMode: true, onApply: () => {} })
    );
    assert(html.includes("data-testid=\"idea-intake-panel\""), "Must render intake panel container");
    assert(html.includes("data-testid=\"intake-toggle-btn\""), "Must render the expand toggle");
    assert(html.includes("aria-expanded=\"false\""), "Panel must be collapsed by default");
    assert(!html.includes("data-testid=\"intake-idea-input\""), "Idea textarea must not render while collapsed");
    assert(!html.includes("<textarea"), "No textarea may render while collapsed (goal stays the first textarea)");
    console.log("  ✓ collapsed intake panel verified");
  }

  // ── Test I2: Expanded panel shows idea input and propose button ───────────
  console.log("\n[Test I2] IdeaIntakePanel expanded state");
  {
    const html = renderToString(
      React.createElement(IdeaIntakePanel, { repoPath: "", chainMode: true, onApply: () => {}, defaultExpanded: true })
    );
    assert(html.includes("data-testid=\"intake-idea-input\""), "Expanded panel must render the idea textarea");
    assert(html.includes("data-testid=\"intake-propose-btn\""), "Expanded panel must render the propose button");
    assert(html.includes("Rozpisz pomysł"), "Propose button must carry its Polish label");
    assert(html.includes("disabled"), "Propose button must be disabled until the idea is long enough");
    console.log("  ✓ expanded intake panel verified");
  }

  // ── Test I3: Result rendering with human markers, risks and forecast ──────
  console.log("\n[Test I3] IntakeResult scope, roadmap, risks and forecast");
  {
    const data = {
      intake_id: "INTAKE_test",
      proposal: {
        goal: "Przypomnienia o wygasających subskrypcjach.",
        first_iteration: "Szkielet endpointu przypomnień",
        done_definition: "Testy przechodzą",
        acceptance_criteria: ["Endpoint zwraca listę"],
        roadmap: [
          { title: "Dodaj harmonogram", kind: "FEATURE", size: "M", human_required: false },
          { title: "Podpnij konto mailowe", kind: "INFRA", size: "S", human_required: true, human_reason: "dane dostępowe" },
        ],
        risks: [{ severity: "HIGH", text: "Brak dostępu do skrzynki nadawcy" }],
      },
      prefill: {
        goal: "Przypomnienia o wygasających subskrypcjach.",
        first_iteration: "Szkielet endpointu przypomnień",
        directions: "Dodaj harmonogram\n[człowiek] Podpnij konto mailowe",
        advanced: { acceptance_criteria: "Endpoint zwraca listę", risks: [{ description: "x", severity: "HIGH", item_ids: [], source: "INTAKE" }] },
      },
      forecast: {
        total: { iterations: 2, covered_items: 1, human_items: 1, unit: "USD", cost: { low: 0.5, mid: 0.8, high: 1.2 }, wall_s: { low: 600, mid: 900, high: 1500 } },
        chains: { length: 8, count: 1, serious_reviews: 1 },
        gates: [{ title: "Podpnij konto mailowe", reason: "wymaga Twojej decyzji" }],
        reach: { enough: false, text: "Za mało zakończonych zadań (1 z 3), by powiedzieć, jak daleko AAW dochodzi bez człowieka." },
        warnings: ["Widełki obejmują 1 z 2 punktów — dla reszty brak danych z historii."],
        note: "Widełki są celowo szerokie.",
      },
    };
    const html = renderToString(React.createElement(IntakeResult, { data: data as any, applied: false, onApply: () => {} }));
    assert(html.includes("data-testid=\"intake-result\""), "Must render result container");
    assert(html.includes("Przypomnienia o wygasających subskrypcjach."), "Must render the goal");
    assert(html.includes("Pierwsza iteracja"), "Must render the first iteration");
    assert(html.includes("data-testid=\"intake-roadmap\""), "Must render roadmap list");
    assert(html.includes("[człowiek]"), "Human-required roadmap items must carry the [człowiek] marker");
    assert(html.includes("Brak dostępu do skrzynki nadawcy"), "Must render risks");
    assert(html.includes("wysokie"), "Must render risk severity label");
    assert(html.includes("Za mało zakończonych zadań"), "Must render honest horizon text");
    assert(html.includes("data-testid=\"intake-cost\""), "Must render the cost line");
    assert(html.includes("$0.50"), "Cost range must be formatted in USD");
    assert(html.includes("data-testid=\"intake-apply-btn\""), "Must render apply button when a prefill exists");
    assert(!html.includes("data-testid=\"intake-applied-note\""), "Applied note must be hidden before apply");
    console.log("  ✓ intake result rendering verified");
  }

  // ── Test I4: Result empty states stay honest ──────────────────────────────
  console.log("\n[Test I4] IntakeResult honest empty states");
  {
    const html = renderToString(
      React.createElement(IntakeResult, {
        data: { proposal: { goal: "Cel testowy", roadmap: [], risks: [] }, prefill: null, forecast: { total: { iterations: 0, cost: null }, reach: { enough: false, text: "" } } } as any,
        applied: false,
        onApply: () => {},
      })
    );
    assert(html.includes("data-testid=\"intake-roadmap-empty\""), "Empty roadmap must say so");
    assert(html.includes("data-testid=\"intake-risks-empty\""), "Empty risks must say so");
    assert(html.includes("Za mało danych z historii, by podać koszt całego planu."), "Missing cost must not show numbers");
    assert(!html.includes("data-testid=\"intake-apply-btn\""), "No prefill means no apply button");
    assert(formatRange(null, "USD") === null, "formatRange must return null for missing ranges");
    console.log("  ✓ intake empty states verified");
  }

  // ── Test F1: Forecast empty and thin-history states ───────────────────────
  console.log("\n[Test F1] ForecastView empty and loading states");
  {
    const empty = renderToString(
      React.createElement(ForecastView, { data: null, appliedProfileId: null, onApplyRecommended: () => {}, onUndoRecommended: () => {} })
    );
    assert(empty.includes("data-testid=\"start-forecast-empty\""), "No data must render the empty state");
    assert(empty.includes("Wpisz cel"), "Empty state must ask for a goal");

    const loading = renderToString(
      React.createElement(ForecastView, { data: null, loading: true, appliedProfileId: null, onApplyRecommended: () => {}, onUndoRecommended: () => {} })
    );
    assert(loading.includes("data-testid=\"start-forecast-loading\""), "Loading must say it is counting");

    const failed = renderToString(
      React.createElement(ForecastView, { data: null, error: "Planista niedostępny", appliedProfileId: null, onApplyRecommended: () => {}, onUndoRecommended: () => {} })
    );
    assert(failed.includes("Planista niedostępny"), "Backend errors must be shown verbatim");
    console.log("  ✓ forecast empty, loading and error states verified");
  }

  // ── Test F2: Recommendation apply and undo controls ───────────────────────
  console.log("\n[Test F2] ForecastView recommended model apply and undo");
  {
    const data = {
      kind: "FEATURE",
      kind_label: "Nowe funkcje",
      n: 5,
      unit: "USD",
      per_iteration: { low: 0.1, mid: 0.2, high: 0.3 },
      recommended: {
        profile_id: "impl-b",
        label: "Model B",
        rate: 0.9,
        n: 6,
        confidence: "RELIABLE",
        apply: { differs: true, runnable: true, mode: "SLOT", current_profile_id: "impl-a" },
      },
      roadmap: {
        reach: { enough: false, text: "Za mało zakończonych zadań." },
        total: { iterations: 3, human_items: 0, unit: "USD", cost: { low: 1, mid: 2, high: 3 }, wall_s: null },
        warnings: [],
      },
    };

    const offer = renderToString(
      React.createElement(ForecastView, { data, appliedProfileId: null, onApplyRecommended: () => {}, onUndoRecommended: () => {} })
    );
    assert(offer.includes("data-testid=\"start-forecast-data\""), "Data must render the forecast body");
    assert(offer.includes("Model B"), "Must name the recommended model");
    assert(offer.includes("90%"), "Must show solve rate as a percentage");
    assert(offer.includes("data-testid=\"apply-recommended-model-btn\""), "Must offer the apply button when the model differs");
    assert(!offer.includes("data-testid=\"undo-recommended-model-btn\""), "Undo must not show before applying");

    const applied = renderToString(
      React.createElement(ForecastView, { data, appliedProfileId: "impl-b", onApplyRecommended: () => {}, onUndoRecommended: () => {} })
    );
    assert(applied.includes("data-testid=\"undo-recommended-model-btn\""), "Applied state must offer undo");
    assert(!applied.includes("data-testid=\"apply-recommended-model-btn\""), "Applied state must not offer apply again");

    const unrunnable = renderToString(
      React.createElement(ForecastView, {
        data: { ...data, recommended: { ...data.recommended, apply: { ...data.recommended.apply, runnable: false } } },
        appliedProfileId: null,
        onApplyRecommended: () => {},
        onUndoRecommended: () => {},
      })
    );
    assert(unrunnable.includes("data-testid=\"start-forecast-not-runnable\""), "Unrunnable model must be reported");
    assert(!unrunnable.includes("data-testid=\"apply-recommended-model-btn\""), "Unrunnable model must not be applicable");
    console.log("  ✓ recommendation apply/undo controls verified");
  }

  console.log("\n✓ Idea intake & start forecast tests passed");
}
