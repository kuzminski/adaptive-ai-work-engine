/**
 * AAW Agentic Studio — Experience View Tests
 *
 * Renders ExperienceReport with representative real, demo and empty payloads (shape of
 * GET /api/experience, see aaw_experience.benchmark / horizon / recommendations) and checks
 * key content, provenance labelling, Pareto/reliability markers and empty states.
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { ExperienceReport, ExperienceView } from "../components/views/ExperienceView";

const realPayload = {
  exploration: null,
  demo: false,
  persona: null,
  personas: { backend: "Inżynier backendu", web: "Twórca stron" },
  scope: "implementation",
  benchmark: {
    schema: "AAW_EXPERIENCE_V1",
    scope: "implementation",
    basis: "usd",
    unit: "USD",
    rows: 24,
    mix: { BUGFIX: 0.6, TESTS: 0.4 },
    kinds: {
      BUGFIX: {
        kind: "BUGFIX",
        label: "Błędy",
        n: 18,
        best: "GPT6_LUNA_HIGH",
        confidence: "RELIABLE",
        pareto: ["GPT6_LUNA_HIGH", "CLAUDE_SONNET_5_5_MEDIUM"],
        points: [
          {
            profile_id: "GPT6_LUNA_HIGH", label: "GPT-6 Luna / high", n: 10, solved: 9, rate: 0.9,
            rate_ci95: [0.6, 0.98], cost_mean: 0.02, cost_per_solved: 0.022, confidence: "RELIABLE",
            on_pareto: true, is_best: true, explored_n: 0, model: "gpt-6-luna",
          },
          {
            profile_id: "CLAUDE_SONNET_5_5_MEDIUM", label: "Claude Sonnet 5.5 / medium", n: 5, solved: 3, rate: 0.6,
            rate_ci95: [0.23, 0.88], cost_mean: 0.3, cost_per_solved: 0.5, confidence: "PRELIMINARY",
            on_pareto: true, is_best: false, explored_n: 1, model: "claude",
          },
          {
            profile_id: "SOL_HIGH", label: "GPT-5.6 Sol / high", n: 2, solved: 2, rate: 1.0,
            rate_ci95: [0.34, 1.0], cost_mean: 0.1, cost_per_solved: 0.1, confidence: "TOO_FEW",
            on_pareto: false, is_best: false, explored_n: 0, model: "sol",
          },
        ],
      },
      TESTS: {
        kind: "TESTS",
        label: "Testy",
        n: 6,
        best: null,
        confidence: "PRELIMINARY",
        pareto: [],
        points: [
          {
            profile_id: "GPT6_LUNA_HIGH", label: "GPT-6 Luna / high", n: 6, solved: 0, rate: 0.0,
            rate_ci95: [0.0, 0.39], cost_mean: 0.05, cost_per_solved: null, confidence: "PRELIMINARY",
            on_pareto: false, is_best: false, explored_n: 0, model: "gpt-6-luna",
          },
        ],
      },
    },
    overall: [
      { profile_id: "GPT6_LUNA_HIGH", label: "GPT-6 Luna / high", coverage: 0.6, cost_per_solved: 0.03, rate: 0.8, is_best: true },
    ],
    warnings: ["Ceny to niezweryfikowane ceny katalogowe API; abonament rozlicza limit, nie tokeny."],
  },
  recommendations: [
    {
      type: "SWITCH", kind: "BUGFIX", label: "Błędy", from: "CLAUDE_SONNET_5_5_MEDIUM", to: "GPT6_LUNA_HIGH",
      saving: 0.56, text: "Błędy: GPT-6 Luna / high wypada taniej niż Claude Sonnet 5.5 / medium.",
      evidence: { n_best: 10, n_incumbent: 5, confidence: "PRELIMINARY" },
    },
    {
      type: "REVIEW_SHARE",
      text: "62% kosztu to review. Dłuższe łańcuchy obniżą rachunek najmocniej.",
      evidence: { REVIEW: 0.62 },
    },
  ],
  horizon: {
    runs: 5,
    enough: true,
    outcomes: { FINISHED: 3, ESCALATED: 1, CAP: 1 },
    finished_share: 0.6,
    iterations: { median: 6, p25: 4, p75: 9, max: 40 },
    stop_reasons: { REVIEW_ESCALATED: 1 },
    by_size: {
      "1-3": { runs: 2, finished_share: 1.0 },
      "4-8": { runs: 2, finished_share: 0.5 },
      "9+": { runs: 1, finished_share: 0.0 },
    },
    text: "3 z 5 zadań doszło do końca w jednym ciągu, mediana 6 iteracji; 1 wymagało człowieka (najczęściej: REVIEW_ESCALATED).",
  },
  kinds: { BUGFIX: "Błędy", TESTS: "Testy" },
  runs: [],
  structure: null,
};

const demoPayload = {
  ...realPayload,
  demo: true,
  persona: "backend",
  horizon: { runs: 6, enough: false, text: "Za mało zakończonych zadań (2 z 3), by powiedzieć, jak daleko AAW dochodzi bez człowieka." },
};

const emptyPayload = {
  demo: false,
  persona: null,
  personas: {},
  benchmark: { schema: "AAW_EXPERIENCE_V1", scope: "implementation", basis: "usd", unit: "USD", rows: 0, mix: {}, kinds: {}, overall: [], warnings: [] },
  recommendations: [],
  horizon: { runs: 0, enough: false, text: "Za mało zakończonych zadań (0 z 3), by powiedzieć, jak daleko AAW dochodzi bez człowieka." },
  kinds: {},
  runs: [],
  structure: null,
};

export async function runExperienceViewTests() {
  console.log("\n=== Starting AAW Experience View Tests ===");

  // ── Test E1: loading state on first render (fetch runs in useEffect) ──────
  console.log("\n[Test E1] ExperienceView initial render shows loading state");
  {
    const html = renderToString(React.createElement(ExperienceView));
    assert(html.includes("data-testid=\"experience-view\""), "Must render view container");
    assert(html.includes("data-testid=\"experience-loading\""), "Must show loading state before data arrives");
    assert(html.includes("data-testid=\"experience-persona-select\""), "Must render source selector");
    assert(html.includes("data-testid=\"experience-refresh-btn\""), "Must render refresh button");
    assert(!html.includes("data-testid=\"experience-scatter-svg\""), "Must not render a chart before data");
    console.log("  ✓ loading state and controls verified");
  }

  // ── Test E2: real data provenance, kinds, scatter, table, recommendations ─
  console.log("\n[Test E2] ExperienceReport with real data");
  {
    const html = renderToString(React.createElement(ExperienceReport, { data: realPayload, selectedKind: "BUGFIX" }));
    assert(html.includes("data-testid=\"experience-report\""), "Must render report container");
    assert(html.includes("data-testid=\"experience-real-badge\""), "Must show REAL data badge");
    assert(!html.includes("data-testid=\"experience-demo-badge\""), "Must NOT show demo badge for real data");
    assert(html.includes("data-testid=\"experience-iteration-count\""), "Must show iteration count");
    assert(html.includes("24"), "Must show the number of decided iterations");
    assert(html.includes("data-testid=\"experience-kind-BUGFIX\""), "Must render BUGFIX kind tab");
    assert(html.includes("data-testid=\"experience-kind-TESTS\""), "Must render TESTS kind tab");
    assert(html.includes("data-testid=\"experience-kind-panel\""), "Must render active kind panel");
    assert(html.includes("data-testid=\"experience-scatter-svg\""), "Must render inline SVG scatter plot");
    assert(html.includes("<title"), "Scatter points must carry <title> for accessibility");
    assert(html.includes("Koszt na rozwiązane zadanie (USD"), "X axis must carry unit label");
    assert(html.includes("Skuteczność (% rozwiązanych iteracji)"), "Y axis must carry label");
    assert(html.includes("data-testid=\"experience-point-GPT6_LUNA_HIGH\""), "Must render point for GPT6 profile");
    assert(html.includes("data-pareto=\"true\""), "Pareto-front points must be marked");
    assert(html.includes("data-testid=\"experience-point-SOL_HIGH\""), "Must render point for low-sample profile");
    assert(html.includes("data-confidence=\"TOO_FEW\""), "Low-sample profile must carry TOO_FEW confidence");
    assert(html.includes("mało danych"), "Low-sample profile must be labelled as too little data");
    assert(html.includes("data-testid=\"experience-pareto-line\""), "Pareto front must be drawn");
    assert(html.includes("$0.022"), "Cost per solved must be formatted in USD");
    assert(html.includes("data-testid=\"experience-profile-table\""), "Must render profile table");
    assert(html.includes("data-testid=\"experience-profile-row-CLAUDE_SONNET_5_5_MEDIUM\""), "Must render table row per profile");
    assert(html.includes("data-testid=\"experience-best-star-GPT6_LUNA_HIGH\""), "Recommended profile must carry star");
    assert(!html.includes("data-testid=\"experience-best-star-CLAUDE_SONNET_5_5_MEDIUM\""), "Non-recommended profile must not carry star");
    assert(html.includes("GPT-6 Luna / high"), "Must render profile label");
    assert(html.includes("data-testid=\"experience-recommendations\""), "Must render recommendations list");
    assert(html.includes("Zmiana modelu"), "SWITCH recommendation must render with its title");
    assert(html.includes("wypada taniej niż Claude Sonnet 5.5 / medium"), "Recommendation reason text must be shown verbatim");
    assert(html.includes("62% kosztu to review"), "Global recommendation must be shown for any kind");
    assert(html.includes("data-testid=\"experience-horizon\""), "Must render horizon section");
    assert(html.includes("3 z 5 zadań doszło do końca"), "Must render horizon text");
    assert(html.includes("data-testid=\"experience-horizon-finished\""), "Must render horizon finished share");
    assert(html.includes("data-testid=\"experience-overall-best\""), "Must render overall best under the mix");
    assert(html.includes("data-testid=\"experience-warnings\""), "Must render benchmark warnings");
    console.log("  ✓ real-data provenance, scatter, table, recommendations and horizon verified");
  }

  // ── Test E3: a kind with no solved profile shows an honest empty chart ───
  console.log("\n[Test E3] ExperienceReport kind without solved runs");
  {
    const html = renderToString(React.createElement(ExperienceReport, { data: realPayload, selectedKind: "TESTS" }));
    assert(html.includes("data-testid=\"experience-scatter-empty\""), "Must show empty chart notice when nothing was solved");
    assert(!html.includes("data-testid=\"experience-scatter-svg\""), "Must not draw an empty chart");
    assert(html.includes("data-testid=\"experience-profile-row-GPT6_LUNA_HIGH\""), "Table must still list profiles");
    assert(!html.includes("data-testid=\"experience-rec-SWITCH-0\""), "Must not show kind-specific SWITCH of another kind");
    assert(html.includes("data-testid=\"experience-rec-REVIEW_SHARE-0\""), "Must still show global recommendation");
    console.log("  ✓ empty chart for unsolved kind verified");
  }

  // ── Test E4: demo data is labelled and never presented as real ───────────
  console.log("\n[Test E4] ExperienceReport with demo persona");
  {
    const html = renderToString(React.createElement(ExperienceReport, { data: demoPayload, selectedKind: "BUGFIX" }));
    assert(html.includes("data-testid=\"experience-demo-badge\""), "Must show demo badge");
    assert(html.includes("PRZYKŁAD / DEMO"), "Demo badge must say example / demo");
    assert(!html.includes("data-testid=\"experience-real-badge\""), "Demo must NOT carry the real-data badge");
    assert(html.includes("Za mało zakończonych zadań"), "Demo horizon with too few runs must say so");
    assert(!html.includes("data-testid=\"experience-horizon-finished\""), "Insufficient horizon must not show stats");
    console.log("  ✓ demo labelling verified");
  }

  // ── Test E5: empty real data gives an honest empty state, no fake numbers ─
  console.log("\n[Test E5] ExperienceReport with no real runs");
  {
    const html = renderToString(React.createElement(ExperienceReport, { data: emptyPayload }));
    assert(html.includes("data-testid=\"experience-empty\""), "Must show empty state");
    assert(html.includes("Za mało danych"), "Empty state must say there is too little data");
    assert(html.includes("data-testid=\"experience-real-badge\""), "Real (empty) data must still be labelled real");
    assert(!html.includes("data-testid=\"experience-scatter-svg\""), "Must not draw a chart without data");
    assert(!html.includes("data-testid=\"experience-kind-panel\""), "Must not render a kind panel without data");
    assert(html.includes("data-testid=\"experience-recommendations-empty\""), "Must show empty recommendations");
    assert(html.includes("Za mało zakończonych zadań (0 z 3)"), "Must show horizon too-little-data text");
    console.log("  ✓ empty state verified");
  }

  console.log("\n=== ALL EXPERIENCE VIEW TESTS PASSED ===");
}
