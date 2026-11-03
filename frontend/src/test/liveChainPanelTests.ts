/**
 * AAW Agentic Studio — Live Chain Panel & Settlement Card Tests
 *
 * Validates (renderToString, no server):
 * - LiveChainPanel: journey stepper, chain slots with review modes, meter with basis,
 *   forecast range and the honest "no estimate" note, deferred findings, collapsible feed
 * - LiveChainPanel: error note and empty states never crash
 * - SettlementCard: actual cost, saving text, forecast comparison, per-category block,
 *   and honest empty states
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { LiveChainPanel } from "../components/live/LiveChainPanel";
import { SettlementCard } from "../components/gate/SettlementCard";

const FULL_LIVE = {
  journey: [
    { id: "idea", label: "Pomysł i mandat", state: "done", detail: "zamrożony kierunek" },
    { id: "charter", label: "Plan początkowy", state: "done", detail: "najsilniejszy model ustala kierunek" },
    { id: "chains", label: "Łańcuchy implementacji", state: "active", detail: "1 zamkniętych" },
    { id: "close", label: "Review serii", state: "pending", detail: "mocny model patrzy na cały łańcuch" },
    { id: "polish", label: "Polerowanie", state: "pending", detail: "2 uwag w kolejce" },
    { id: "gate", label: "Twoja decyzja", state: "pending", detail: "bez merge i push" },
  ],
  chain: {
    chain_id: 2,
    length: 4,
    closed: 1,
    slots: [
      { position: 1, closing: false, state: "accepted", iteration: 5, cost: 0.02, explored: false, goal: "Krok A" },
      { position: 2, closing: false, state: "provisional", iteration: 6, cost: 0.03, explored: true, goal: "Krok B" },
      { position: 3, closing: false, state: "active", iteration: 7, cost: null, explored: false, goal: "Krok C" },
      { position: 4, closing: true, state: "pending", iteration: null, cost: null, explored: false, goal: null },
    ],
  },
  meter: {
    calls: 12,
    input_tokens: 1000,
    cached_tokens: 100,
    output_tokens: 200,
    proxy_units: 5400,
    cost_usd: 0.1234,
    wall_s: 321,
    by_category: { PLANNING: 1000, IMPLEMENTATION: 3000, VERIFICATION: 800, REVIEW: 600 },
    share: {},
    failed_calls: 1,
    unpriced_calls: 0,
  },
  forecast: {
    pending_items: 3,
    open_ended: false,
    chains_closed: 1,
    polish_expected: true,
    estimate: {
      source: "TEN PRZEBIEG",
      n: 4,
      unit: "USD",
      iterations_left: 3,
      cost: { low: 0.05, mid: 0.09, high: 0.14 },
      wall_s: { low: 300, mid: 450, high: 700 },
    },
    note: null,
  },
  feed: [
    { at: "2026-11-03T10:15:30+00:00", event: "ITERATION_ACCEPTED", text: "Iteracja zaakceptowana", phase: "REVIEW" },
    { at: "2026-11-03T10:10:00+00:00", event: "CHAIN_CLOSE_STARTED", text: "Zaczyna się review serii", phase: null },
  ],
  now: { executor: "claude", role: "implementer", started_at: "2026-11-03T10:16:00+00:00", profile: "SOL_HIGH" },
  deferred_open: 2,
  error: null,
};

const EMPTY_LIVE = {
  journey: [],
  chain: null,
  meter: { calls: 0, cost_usd: null, proxy_units: null, wall_s: null, failed_calls: 0, unpriced_calls: 0 },
  forecast: { estimate: null, note: "Za mało danych, by uczciwie oszacować czas i koszt — pokażę je po kilku iteracjach." },
  feed: [],
  now: null,
  deferred_open: 0,
};

const SETTLEMENT_SAVED = {
  unit: "USD",
  actual: 0.1234,
  items: [{ kind: "BUGFIX", from: "GPT6_LUNA_MAX", to: "GPT6_LUNA_HIGH", saving_ratio: 0.42, saving: 0.02 }],
  saving: { low: 0.0104, high: 0.0208 },
};

const SETTLEMENT_NO_SAVING = {
  unit: "proxy",
  actual: 5400,
  items: [],
  saving: null,
};

export async function runLiveChainPanelTests() {
  console.log("\n=== Starting AAW Live Chain Panel & Settlement Card Tests ===");

  // ── Test LC-1: full live payload renders every block ─────────────────────
  console.log("\n[Test LC-1] LiveChainPanel full payload");
  {
    const html = renderToString(React.createElement(LiveChainPanel, { live: FULL_LIVE, status: "RUNNING" }));
    assert(html.includes('data-testid="live-chain-panel"'), "panel container must render");
    assert(html.includes('data-testid="live-journey"'), "journey stepper must render");
    assert(html.includes('data-testid="live-journey-step-chains"'), "journey step must render");
    assert(html.includes('data-state="active"'), "active journey step must carry its state");
    assert(html.includes("Pomysł i mandat"), "journey labels must render");
    assert(html.includes('data-testid="live-chain"'), "chain block must render");
    assert(html.includes('data-testid="live-chain-slot-1"'), "chain slot 1 must render");
    assert(html.includes('data-state="accepted"'), "accepted slot must map to accepted state");
    assert(html.includes('data-state="provisional"'), "provisional slot must render");
    assert(html.includes('data-state="active"') && html.includes('data-testid="live-chain-slot-3"'), "active slot must render");
    assert(html.includes('data-review-mode="CHAIN_CLOSE"'), "closing slot must be CHAIN_CLOSE");
    assert(html.includes('data-review-mode="LIGHT"'), "non-closing slots must be LIGHT");
    assert(html.includes("eksploracja"), "explored slot must be labelled");
    assert(html.includes('data-testid="live-meter"'), "meter must render");
    assert(html.includes('data-testid="live-meter-spent"'), "spent value must render");
    assert(html.includes("$0.1234"), "USD spent must render as USD when priced");
    assert(html.includes('data-testid="live-meter-basis"') && html.includes("jednostka: USD"), "basis label must render");
    assert(html.includes('data-testid="live-forecast"'), "forecast block must render");
    assert(html.includes('data-testid="live-forecast-cost"'), "forecast range must render when estimate exists");
    assert(html.includes("$0.0500") && html.includes("$0.1400"), "forecast low and high must render");
    assert(html.includes("źródło: ten przebieg"), "forecast source must render");
    assert(html.includes('data-testid="live-deferred"') && html.includes("odłożone na polerowanie: 2"), "deferred count must render");
    assert(html.includes('data-testid="live-feed"'), "feed must render as a collapsible block");
    assert(html.includes("<details"), "feed must use a details element");
    assert(html.includes('data-testid="live-feed-item"'), "feed items must render");
    assert(html.includes('data-testid="live-now"'), "current step must render when in flight");
    assert(html.includes('data-testid="live-polish-badge"') === false, "POLISH badge only while polishing");
    assert(html.includes('data-testid="live-status"') && html.includes("RUNNING"), "status must render");
  }

  // ── Test LC-2: empty and no-estimate states are honest, never crash ──────
  console.log("\n[Test LC-2] LiveChainPanel empty states");
  {
    const html = renderToString(React.createElement(LiveChainPanel, { live: EMPTY_LIVE }));
    assert(html.includes('data-testid="live-journey-empty"'), "empty journey must show note");
    assert(html.includes('data-testid="live-chain-empty"'), "missing chain must show note");
    assert(html.includes('data-testid="live-meter-empty"'), "zero calls must show low-data note");
    assert(html.includes('data-testid="live-forecast-empty"'), "no estimate must show note");
    assert(html.includes("Za mało danych, by uczciwie oszacować"), "backend no-estimate note must render verbatim");
    assert(html.includes('data-testid="live-forecast-cost"') === false, "no fake forecast range");
    assert(html.includes("Brak drobnych uwag odłożonych na polerowanie"), "zero deferred must render honest text");
    assert(html.includes('data-testid="live-feed-empty"'), "empty feed must render note");
    assert(html.includes('data-testid="live-now"') === false, "no current step when idle");
  }

  // ── Test LC-3: error note and absent payload ─────────────────────────────
  console.log("\n[Test LC-3] LiveChainPanel error and absent payload");
  {
    const errHtml = renderToString(
      React.createElement(LiveChainPanel, { live: { error: "RuntimeError: boom" } as any })
    );
    assert(errHtml.includes('data-testid="live-error-note"'), "error must show muted note");
    assert(errHtml.includes("RuntimeError: boom"), "error text must render");
    assert(errHtml.includes('data-testid="live-journey"') === false, "error must not render empty blocks");

    const nullHtml = renderToString(React.createElement(LiveChainPanel, { live: null as any }));
    assert.strictEqual(nullHtml, "", "absent live payload renders nothing");

    const partialHtml = renderToString(React.createElement(LiveChainPanel, { live: { journey: null, feed: "bad" } as any }));
    assert(partialHtml.includes('data-testid="live-chain-panel"'), "malformed fields must not crash the panel");
  }

  // ── Test SC-1: settlement with saving and per-category block ─────────────
  console.log("\n[Test SC-1] SettlementCard with saving");
  {
    const html = renderToString(
      React.createElement(SettlementCard, {
        settlement: { ...SETTLEMENT_SAVED, by_category: { planning: 0.01, implementation: 0.08 } },
      })
    );
    assert(html.includes('data-testid="settlement-card"'), "settlement card must render");
    assert(html.includes("Koszt zadania: $0.1234"), "actual cost must render");
    assert(html.includes('data-testid="settlement-saving"') && html.includes("Mogło kosztować mniej"), "saving range must render");
    assert(html.includes("$0.0104") && html.includes("$0.0208"), "saving low and high must render");
    assert(html.includes('data-testid="settlement-item"') && html.includes("BUGFIX"), "switch item must render");
    assert(html.includes('data-testid="settlement-categories"'), "category block must render when present");
    assert(html.includes('data-testid="settlement-category-planning"'), "planning category must render");
    assert(html.includes("Implementacja"), "category label must render");
    assert(html.includes('data-testid="settlement-forecast-empty"'), "no forecast means honest note");
  }

  // ── Test SC-2: no saving, no categories, forecast comparison ─────────────
  console.log("\n[Test SC-2] SettlementCard without saving and with forecast");
  {
    const noSave = renderToString(React.createElement(SettlementCard, { settlement: SETTLEMENT_NO_SAVING }));
    assert(noSave.includes("Brak oszczędności do pokazania"), "no saving must say so honestly");
    assert(noSave.includes("5400 proxy"), "proxy unit must render without USD sign");
    assert(noSave.includes('data-testid="settlement-categories"') === false, "no category block when absent");

    const withForecast = renderToString(
      React.createElement(SettlementCard, {
        settlement: SETTLEMENT_SAVED,
        forecast: FULL_LIVE.forecast,
      })
    );
    assert(withForecast.includes('data-testid="settlement-delta"'), "delta must render when forecast matches the unit");
    assert(withForecast.includes("Prognoza z podglądu na żywo"), "forecast source must be named");

    const mismatch = renderToString(
      React.createElement(SettlementCard, {
        settlement: SETTLEMENT_NO_SAVING,
        forecast: FULL_LIVE.forecast,
      })
    );
    assert(mismatch.includes('data-testid="settlement-forecast-empty"'), "unit mismatch must not compare");
    assert(mismatch.includes('data-testid="settlement-delta"') === false, "no delta across units");
  }

  // ── Test SC-3: absent settlement renders nothing ─────────────────────────
  console.log("\n[Test SC-3] SettlementCard absent payload");
  {
    const html = renderToString(React.createElement(SettlementCard, { settlement: null }));
    assert.strictEqual(html, "", "absent settlement renders nothing");
  }

  console.log("  ✓ LiveChainPanel and SettlementCard render checks passed");
}
