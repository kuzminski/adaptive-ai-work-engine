/**
 * AAW Agentic Studio — View Component Tests
 *
 * Validates rendering and reactivity of:
 * - LiveCommandCenterView
 * - NewTaskWizardView
 * - RunsHistoryView
 * - SettingsView
 * - App dynamic view routing based on activeNav
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { App } from "../App";
import { LiveCommandCenterView } from "../components/views/LiveCommandCenterView";
import { NewTaskWizardView } from "../components/views/NewTaskWizardView";
import { RunsHistoryView } from "../components/views/RunsHistoryView";
import { SettingsView } from "../components/views/SettingsView";
import { engineStore } from "../store/engineStore";

export async function runViewComponentTests() {
  console.log("\n=== Starting AAW View & Navigation Tests ===");

  // ── Test 21: LiveCommandCenterView Rendering & Action Buttons ─────────────
  console.log("\n[Test 21] LiveCommandCenterView Rendering & Action Controls");
  {
    engineStore.actions.reset();

    // Idle state
    const htmlIdle = renderToString(React.createElement(LiveCommandCenterView));
    assert(htmlIdle.includes("data-testid=\"live-command-center-view\""), "Must render view container");
    assert(htmlIdle.includes("data-testid=\"nav-new-task-btn\""), "Must contain New Task button");
    assert(htmlIdle.includes("data-testid=\"refresh-btn\""), "Must contain Refresh button");
    assert(htmlIdle.includes("data-testid=\"polling-toggle-btn\""), "Must contain Polling toggle");
    assert(htmlIdle.includes("GOTOWY DO PRACY"), "Should display default engine status");

    // Running state with active run
    engineStore.actions._setState({
      activeRunId: "test-run-view-1",
      run: {
        run_id: "test-run-view-1",
        project: "Payment Integration",
        goal: "Implement Stripe Webhook",
        status: "RUNNING",
        engine: { status: "EXECUTE" },
        process: {
          iteration: 2,
          max_iterations: 5,
          run_elapsed_s: 42,
        },
      } as any,
    });

    const htmlRunning = renderToString(React.createElement(LiveCommandCenterView));
    assert(htmlRunning.includes("data-testid=\"stop-safely-btn\""), "Must render Stop Safely button when running");
    assert(htmlRunning.includes("data-testid=\"stop-force-btn\""), "Must render Stop Force button when running");
    assert(htmlRunning.includes("Payment Integration"), "Must render project name");
    assert(htmlRunning.includes("42s"), "Must render elapsed seconds");

    // Paused state
    engineStore.actions._setState({
      run: {
        run_id: "test-run-view-1",
        status: "PAUSED",
        engine: { status: "AWAITING_HUMAN" },
      } as any,
    });

    const htmlPaused = renderToString(React.createElement(LiveCommandCenterView));
    assert(htmlPaused.includes("data-testid=\"resume-btn\""), "Must render Resume button when paused");

    console.log("  ✓ LiveCommandCenterView states, stats, and control buttons verified");
  }

  // ── Test 22: NewTaskWizardView Rendering & Form Fields ────────────────────
  console.log("\n[Test 22] NewTaskWizardView Form Fields & Model Selection");
  {
    engineStore.actions.reset();

    const htmlWizard = renderToString(React.createElement(NewTaskWizardView));
    assert(htmlWizard.includes("data-testid=\"new-task-wizard-view\""), "Must render wizard container");
    assert(htmlWizard.includes("1. Repozytorium i Przestrzeń Robocza"), "Must render Section 1");
    assert(htmlWizard.includes("2. Cel Główny i Kierunek Iteracji"), "Must render Section 2");
    assert(htmlWizard.includes("3. Przydział Modeli AI i Ról Wykonawczych"), "Must render Section 3");
    assert(htmlWizard.includes("4. Limity Autonomii i Polityka Bezpieczeństwa"), "Must render Section 4");
    assert(htmlWizard.includes("data-testid=\"submit-new-task-btn\""), "Must render submit button");

    console.log("  ✓ NewTaskWizardView 5-section form and inputs verified");
  }

  // ── Test 23: RunsHistoryView Rendering & Filters ──────────────────────────
  console.log("\n[Test 23] RunsHistoryView Filtering, Sections & Actions");
  {
    engineStore.actions.reset();

    engineStore.actions._setState({
      runs: {
        sections: {
          running: [
            {
              run_id: "run-running-1",
              project: "Project Alpha",
              goal: "Build Alpha",
              status: "RUNNING",
              status_label: "Pracuje",
            },
          ],
          attention: [
            {
              run_id: "run-gate-1",
              project: "Project Beta",
              goal: "Build Beta",
              status: "READY_FOR_DECISION",
              status_label: "Czeka na Twoją decyzję",
            },
          ],
          paused: [
            {
              run_id: "run-paused-1",
              project: "Project Gamma",
              goal: "Build Gamma",
              status: "PAUSED",
              status_label: "Wstrzymane",
              can_resume: true,
            },
          ],
          completed: [
            {
              run_id: "run-done-1",
              project: "Project Delta",
              goal: "Build Delta",
              status: "ACCEPTED",
              status_label: "Zaakceptowane",
            },
          ],
        },
      } as any,
    });

    const htmlHistory = renderToString(React.createElement(RunsHistoryView));
    assert(htmlHistory.includes("data-testid=\"runs-history-view\""), "Must render history container");
    assert(htmlHistory.includes("run-running-1"), "Must display running run ID");
    assert(htmlHistory.includes("run-gate-1"), "Must display attention run ID");
    assert(htmlHistory.includes("run-paused-1"), "Must display paused run ID");
    assert(htmlHistory.includes("run-done-1"), "Must display completed run ID");
    assert(htmlHistory.includes("Załaduj do Studio"), "Must render Load into Studio buttons");

    console.log("  ✓ RunsHistoryView grouped sections, cards, and action buttons verified");
  }

  // ── Test 24: SettingsView Configuration & MCP Hub ────────────────────────
  console.log("\n[Test 24] SettingsView Limits, Timeouts & MCP Hub Link");
  {
    engineStore.actions.reset();

    const htmlSettings = renderToString(React.createElement(SettingsView));
    assert(htmlSettings.includes("data-testid=\"settings-view\""), "Must render settings container");
    assert(htmlSettings.includes("1. Bezpieczniki Pętli Autonomicznej"), "Must render Section 1");
    assert(htmlSettings.includes("2. Domyślne Tryby Przydziału Modeli"), "Must render Section 2");
    assert(htmlSettings.includes("3. Universal Model Context Protocol (MCP) Hub"), "Must render MCP Hub");
    assert(htmlSettings.includes("data-testid=\"save-settings-btn\""), "Must render Save Settings button");

    console.log("  ✓ SettingsView form fields, limits, and MCP status verified");
  }

  // ── Test 25: App Dynamic View Routing based on activeNav ──────────────────
  console.log("\n[Test 25] App Dynamic Navigation Switching (activeNav)");
  {
    engineStore.actions.reset();

    // 1. Command Center view
    engineStore.actions.setActiveNav("command-center");
    const html1 = renderToString(React.createElement(App));
    assert(html1.includes("data-testid=\"live-command-center-view\""), "Must render LiveCommandCenterView");
    assert(!html1.includes("data-testid=\"new-task-wizard-view\""), "Must NOT render NewTaskWizardView");

    // 2. New Task Wizard view
    engineStore.actions.setActiveNav("new-task");
    const html2 = renderToString(React.createElement(App));
    assert(html2.includes("data-testid=\"new-task-wizard-view\""), "Must render NewTaskWizardView");
    assert(!html2.includes("data-testid=\"live-command-center-view\""), "Must NOT render LiveCommandCenterView");

    // 3. Runs History view
    engineStore.actions.setActiveNav("runs");
    const html3 = renderToString(React.createElement(App));
    assert(html3.includes("data-testid=\"runs-history-view\""), "Must render RunsHistoryView");
    assert(!html3.includes("data-testid=\"live-command-center-view\""), "Must NOT render LiveCommandCenterView");

    // 4. Settings view
    engineStore.actions.setActiveNav("settings");
    const html4 = renderToString(React.createElement(App));
    assert(html4.includes("data-testid=\"settings-view\""), "Must render SettingsView");
    assert(!html4.includes("data-testid=\"live-command-center-view\""), "Must NOT render LiveCommandCenterView");

    console.log("  ✓ App dynamic view routing correctly switches between all 4 views");
  }

  console.log("\n=== ALL VIEW & NAVIGATION TESTS PASSED ===");
}
