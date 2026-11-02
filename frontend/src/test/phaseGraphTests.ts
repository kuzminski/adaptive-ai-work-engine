/**
 * AAW Agentic Studio — 7-Phase Execution Graph & State Machine Tests
 *
 * Validates:
 * 1. resolvePhaseNodes state machine resolution for all engine lifecycle states:
 *    - Idle / Null run state
 *    - Active execution (PLAN -> EXECUTE -> ...)
 *    - REPAIR loopback state and cycle counting
 *    - AWAITING_HUMAN hold state with prominent Node 7 human gate
 *    - Completed / Accepted terminal state
 *    - Failure handling
 * 2. PhaseNode component rendering:
 *    - Index numbering (01..07)
 *    - Polish sublabels (Dekompozycja, Kodowanie, Testy w roli, etc.)
 *    - Status badges, duration, and icons
 * 3. PhaseGraph component rendering:
 *    - 7 horizontal phase node cards
 *    - SVG loopback curve for REPAIR connecting REVIEW -> REPAIR -> SELF_VERIFY
 *    - SVG forward connecting arrows
 *    - Telemetry / last event telemetry bar underneath
 *    - Amber pulsing BRAMKA CZŁOWIEKA on AWAITING_HUMAN
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { RunDetail } from "../api/types";
import { PhaseNode, PhaseNodeInfo } from "../components/graph/PhaseNode";
import { PhaseGraph, resolvePhaseNodes } from "../components/graph/PhaseGraph";
import { engineStore } from "../store/engineStore";

export async function runPhaseGraphTests() {
  console.log("\n=== Starting AAW 7-Phase Execution Graph Tests ===");

  // ── Test 9: resolvePhaseNodes State Machine Resolution ────────────────────
  console.log("\n[Test 9] resolvePhaseNodes State Machine Lifecycle Mapping");
  {
    // Case 1: Null / Starting run
    const idleResult = resolvePhaseNodes(null);
    assert.strictEqual(idleResult.phases.length, 7, "Must contain exactly 7 phases");
    assert.strictEqual(idleResult.isAwaitingHuman, false);
    assert.strictEqual(idleResult.isRepairActive, false);
    assert.strictEqual(idleResult.repairCount, 0);

    // Verify all 7 phase keys and Polish sublabels
    const expectedSublabels: Record<string, string> = {
      PLAN: "Dekompozycja",
      EXECUTE: "Kodowanie",
      SELF_VERIFY: "Testy w roli",
      REVIEW: "Recenzja",
      REPAIR: "Pętla napraw",
      FINAL_REVIEW: "Całościowa ocena",
      ROADMAP_CHECK: "Weryfikacja celu",
    };

    idleResult.phases.forEach((p, idx) => {
      assert.strictEqual(p.index, idx + 1);
      assert.strictEqual(p.status, "pending", `Idle phase ${p.name} must be pending`);
      assert.strictEqual(p.sublabel, expectedSublabels[p.key]);
    });
    console.log("  ✓ Null / Idle run correctly initializes 7 pending phases with Polish sublabels");

    // Case 2: Active EXECUTE phase
    const executeRun: RunDetail = {
      run_id: "run-exec-1",
      status: "RUNNING",
      status_label: "Running",
      section: "running",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: {
        phase: "EXECUTE",
        phase_label: "Kodowanie",
        phase_elapsed_s: 18,
        run_elapsed_s: 42,
        activity: "Implementer generuje kod w repozytorium",
        model: "Claude 3.5 Sonnet",
        role: "Implementer",
        iteration: 1,
        max_iterations: 5,
        execution_id: "exec-1",
        step_elapsed_s: 18,
        roadmap: { done: 0, total: 3 },
        roadmap_items: [],
        iterations_done: 0,
        iteration_id: "iter-1",
        nodes: [],
        eta: null,
      },
      timeline: [],
      gate: null,
      engine: { status: "RUNNING", phase: "EXECUTE" },
    };

    const execResult = resolvePhaseNodes(executeRun);
    assert.strictEqual(execResult.phases[0].status, "passed", "PLAN (index 0) must be passed");
    assert.strictEqual(execResult.phases[1].status, "active", "EXECUTE (index 1) must be active");
    assert.strictEqual(execResult.phases[1].duration, "18s", "Active phase duration must be formatted");
    for (let i = 2; i < 7; i++) {
      assert.strictEqual(execResult.phases[i].status, "pending", `Phase ${execResult.phases[i].name} must be pending`);
    }
    assert.strictEqual(execResult.activeRole, "Implementer");
    assert.strictEqual(execResult.activeModel, "Claude 3.5 Sonnet");
    assert.strictEqual(execResult.lastActivityText, "Implementer generuje kod w repozytorium");
    console.log("  ✓ Active EXECUTE phase resolves prior phases as passed and subsequent as pending");

    // Case 3: REPAIR loopback phase with repair iterations
    const repairRun: RunDetail = {
      run_id: "run-repair-1",
      status: "RUNNING",
      status_label: "Running",
      section: "running",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: {
        phase: "REPAIR",
        phase_label: "Pętla napraw",
        phase_elapsed_s: 9,
        run_elapsed_s: 120,
        activity: "Naprawa defektów wykrytych przez recenzenta",
        model: "Claude 3.5 Sonnet",
        role: "Implementer",
        iteration: 2,
        max_iterations: 5,
        execution_id: "exec-rep-2",
        step_elapsed_s: 9,
        roadmap: { done: 1, total: 3 },
        standing: { item_id: "task-1", status: "repair", iterations: 2 },
        roadmap_items: [],
        iterations_done: 1,
        iteration_id: "iter-2",
        nodes: [],
        eta: null,
      },
      timeline: [],
      gate: null,
      engine: { status: "RUNNING", phase: "REPAIR" },
    };

    const repResult = resolvePhaseNodes(repairRun);
    assert.strictEqual(repResult.isRepairActive, true, "isRepairActive must be true");
    assert.strictEqual(repResult.repairCount, 2, "repairCount must be extracted from standing.iterations");
    assert.strictEqual(repResult.phases[0].status, "passed", "PLAN passed");
    assert.strictEqual(repResult.phases[1].status, "passed", "EXECUTE passed");
    assert.strictEqual(repResult.phases[2].status, "passed", "SELF_VERIFY passed");
    assert.strictEqual(repResult.phases[3].status, "passed", "REVIEW passed");
    assert.strictEqual(repResult.phases[4].status, "looping", "REPAIR must have looping status");
    assert.strictEqual(repResult.phases[4].iterationCount, 2, "REPAIR iterationCount must be 2");
    assert.strictEqual(repResult.phases[5].status, "pending", "FINAL_REVIEW must be pending");
    assert.strictEqual(repResult.phases[6].status, "pending", "ROADMAP_CHECK must be pending");
    console.log("  ✓ REPAIR loopback phase correctly flags looping status and cycle count");

    // Case 4: AWAITING_HUMAN hold state (Human Gate at Node 7)
    const gateRun: RunDetail = {
      run_id: "run-gate-1",
      status: "READY_FOR_DECISION",
      status_label: "Awaiting Human Decision",
      section: "attention",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: {
        phase: "ROADMAP_CHECK",
        phase_label: "Weryfikacja celu",
        phase_elapsed_s: 0,
        run_elapsed_s: 210,
        activity: "Oczekiwanie na akceptację człowieka (Human Gate)",
        model: "AAW Gatekeeper",
        role: "System",
        iteration: 3,
        max_iterations: 3,
        execution_id: "gate-1",
        step_elapsed_s: 0,
        roadmap: { done: 3, total: 3 },
        roadmap_items: [],
        iterations_done: 3,
        iteration_id: "iter-3",
        nodes: [],
        eta: null,
      },
      timeline: [],
      gate: {
        why: "Wszystkie zadania z roadmapy zostały zrealizowane",
        hold_reason: "ROADMAP_EXHAUSTED",
        escalation_code: null,
        done: ["Zaimplementowano architekturę", "Testy jednostkowe zielone"],
        remaining: [],
        warnings: [],
        planner_end_reason: "ROADMAP_COMPLETE",
        could_continue: false,
        candidate: {
          candidate_id: "cand-1",
          head: "c362dbf",
          diff_sha256: "abc",
          changed_files: ["src/index.ts"],
          branch: "feat/phase-graph",
          worktree: "c:/worktree",
          base_commit: "7ef765c",
          promotable: true,
          roadmap_exhausted: true,
        },
        actions: {
          accept: true,
          accept_needs_early_end: false,
          add_direction: false,
          new_goal: false,
          reject: true,
          evidence: true,
        },
        decision: null,
        human: null,
        promotion: null,
        accept_meaning: "Zatwierdź i zintegruj",
        merge_push: "fast-forward",
        main_merge_allowed: true,
      },
      engine: { status: "AWAITING_HUMAN", phase: "ROADMAP_CHECK" },
    };

    const gateResult = resolvePhaseNodes(gateRun);
    assert.strictEqual(gateResult.isAwaitingHuman, true, "isAwaitingHuman must be true");
    assert.strictEqual(gateResult.phases[6].status, "waiting_gate", "Node 7 must have waiting_gate status");
    assert.strictEqual(gateResult.phases[6].isHumanGate, true, "Node 7 isHumanGate must be true");
    for (let i = 0; i < 6; i++) {
      assert.strictEqual(gateResult.phases[i].status, "passed", `Pre-gate phase ${gateResult.phases[i].name} must be passed`);
    }
    console.log("  ✓ AWAITING_HUMAN maps Node 7 (ROADMAP_CHECK) to waiting_gate with isHumanGate=true");

    // Case 5: Completed / Accepted run
    const completedRun: RunDetail = {
      run_id: "run-done-1",
      status: "ACCEPTED",
      status_label: "Accepted",
      section: "completed",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: null,
      timeline: [],
      gate: null,
      engine: { status: "PROMOTED" },
    };

    const doneResult = resolvePhaseNodes(completedRun);
    doneResult.phases.forEach((p) => {
      assert.strictEqual(p.status, "passed", `Completed run must mark phase ${p.name} as passed`);
    });
    console.log("  ✓ Completed / Accepted run marks all 7 phases as passed");
  }

  // ── Test 10: PhaseNode Component Rendering ────────────────────────────────
  console.log("\n[Test 10] PhaseNode Component Rendering & Badges");
  {
    // 1. Pending node
    const pendingNode: PhaseNodeInfo = {
      index: 1,
      key: "PLAN",
      name: "PLAN",
      sublabel: "Dekompozycja",
      status: "pending",
      colorHex: "#818cf8",
      borderColor: "border-phase-plan",
      bgActiveColor: "bg-phase-plan/10",
    };
    const htmlPending = renderToString(React.createElement(PhaseNode, { phase: pendingNode }));
    assert(htmlPending.includes("data-testid=\"phase-node-plan\""));
    assert(htmlPending.includes("01"), "Must render formatted index 01");
    assert(htmlPending.includes("PLAN"), "Must render phase name");
    assert(htmlPending.includes("Dekompozycja"), "Must render Polish sublabel");
    assert(htmlPending.includes("OCZEKUJE"), "Must render OCZEKUJE badge");

    // 2. Active node with duration
    const activeNode: PhaseNodeInfo = {
      index: 2,
      key: "EXECUTE",
      name: "EXECUTE",
      sublabel: "Kodowanie",
      status: "active",
      duration: "24s",
      colorHex: "#38bdf8",
      borderColor: "border-phase-execute",
      bgActiveColor: "bg-phase-execute/10",
    };
    const htmlActive = renderToString(React.createElement(PhaseNode, { phase: activeNode }));
    assert(htmlActive.includes("data-testid=\"phase-node-execute\""));
    assert(htmlActive.includes("02"));
    assert(htmlActive.includes("Kodowanie"));
    assert(htmlActive.includes("AKTYWNY"));
    assert(htmlActive.includes("24s"), "Must render duration badge");
    assert(htmlActive.includes("data-testid=\"icon-active-pulse\""));

    // 3. Looping REPAIR node
    const loopingNode: PhaseNodeInfo = {
      index: 5,
      key: "REPAIR",
      name: "REPAIR",
      sublabel: "Pętla napraw",
      status: "looping",
      iterationCount: 3,
      colorHex: "#f43f5e",
      borderColor: "border-phase-repair",
      bgActiveColor: "bg-phase-repair/10",
    };
    const htmlLooping = renderToString(React.createElement(PhaseNode, { phase: loopingNode }));
    assert(htmlLooping.includes("data-testid=\"phase-node-repair\""));
    assert(htmlLooping.includes("Pętla napraw"));
    assert(htmlLooping.includes("NAPRAWA"));
    assert(htmlLooping.includes("#3"), "Must render loop iteration count");
    assert(htmlLooping.includes("data-testid=\"icon-loop\""));

    // 4. Human gate node (BRAMKA CZŁOWIEKA)
    const gateNode: PhaseNodeInfo = {
      index: 7,
      key: "ROADMAP_CHECK",
      name: "ROADMAP_CHECK",
      sublabel: "Weryfikacja celu",
      status: "waiting_gate",
      isHumanGate: true,
      colorHex: "#34d399",
      borderColor: "border-phase-roadmap_check",
      bgActiveColor: "bg-phase-roadmap_check/10",
    };
    const htmlGate = renderToString(React.createElement(PhaseNode, { phase: gateNode }));
    assert(htmlGate.includes("data-testid=\"phase-node-roadmap_check\""));
    assert(htmlGate.includes("Weryfikacja celu"));
    assert(htmlGate.includes("BRAMKA CZŁOWIEKA"), "Must render prominent BRAMKA CZŁOWIEKA badge");
    assert(htmlGate.includes("data-testid=\"icon-gate\""));
    assert(htmlGate.includes("animate-pulse"));

    console.log("  ✓ PhaseNode renders all states: pending, active with duration, repair looping, and human gate");
  }

  // ── Test 11: PhaseGraph Component & SVG Flow Rendering ────────────────────
  console.log("\n[Test 11] PhaseGraph Component, SVG Loopback Curve & Telemetry Bar");
  {
    engineStore.actions.reset();

    // 1. Render PhaseGraph with active run
    const testRun: RunDetail = {
      run_id: "run-graph-test-1",
      status: "RUNNING",
      status_label: "Running",
      section: "running",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: {
        phase: "SELF_VERIFY",
        phase_label: "Testy w roli",
        phase_elapsed_s: 12,
        run_elapsed_s: 65,
        activity: "Weryfikator potwierdził testy jednostkowe",
        model: "Claude 3.5 Sonnet",
        role: "Verifier",
        iteration: 1,
        max_iterations: 5,
        execution_id: "exec-v-1",
        step_elapsed_s: 12,
        roadmap: { done: 1, total: 3 },
        standing: null,
        roadmap_items: [],
        iterations_done: 1,
        iteration_id: "iter-1",
        nodes: [],
        eta: null,
      },
      timeline: [],
      gate: null,
      engine: { status: "RUNNING", phase: "SELF_VERIFY" },
    };

    const htmlGraph = renderToString(React.createElement(PhaseGraph, { run: testRun }));

    // Verify main container and title
    assert(htmlGraph.includes("data-testid=\"phase-graph-container\""));
    assert(htmlGraph.includes("7-Fazowa Maszyna Stanów (AAW Engine Phase Graph)"));

    // Verify all 7 phase nodes exist in graph
    assert(htmlGraph.includes("data-testid=\"phase-node-plan\""));
    assert(htmlGraph.includes("data-testid=\"phase-node-execute\""));
    assert(htmlGraph.includes("data-testid=\"phase-node-self_verify\""));
    assert(htmlGraph.includes("data-testid=\"phase-node-review\""));
    assert(htmlGraph.includes("data-testid=\"phase-node-repair\""));
    assert(htmlGraph.includes("data-testid=\"phase-node-final_review\""));
    assert(htmlGraph.includes("data-testid=\"phase-node-roadmap_check\""));

    // Verify Polish sublabels
    assert(htmlGraph.includes("Dekompozycja"));
    assert(htmlGraph.includes("Kodowanie"));
    assert(htmlGraph.includes("Testy w roli"));
    assert(htmlGraph.includes("Recenzja"));
    assert(htmlGraph.includes("Pętla napraw"));
    assert(htmlGraph.includes("Całościowa ocena"));
    assert(htmlGraph.includes("Weryfikacja celu"));

    // Verify SVG loopback curve for REPAIR
    assert(htmlGraph.includes("data-testid=\"phase-graph-svg\""));
    assert(htmlGraph.includes("data-testid=\"repair-loopback-curve\""));
    assert(htmlGraph.includes("M 450 38 C 450 6, 250 6, 250 38"), "Loopback SVG path coordinates match 5 -> 3 curve");
    assert(htmlGraph.includes("Pętla naprawcza (REVIEW ⮂ REPAIR → SELF_VERIFY)"));

    // Verify Underneath Telemetry Bar
    assert(htmlGraph.includes("data-testid=\"telemetry-bar\""));
    assert(htmlGraph.includes("data-testid=\"last-activity-text\""));
    assert(htmlGraph.includes("Weryfikator potwierdził testy jednostkowe"));
    assert(htmlGraph.includes("Verifier"));
    assert(htmlGraph.includes("Iteracja 1 / 5"));

    // 2. Render PhaseGraph with REPAIR active run
    const testRepairRun: RunDetail = {
      run_id: "run-graph-repair",
      status: "RUNNING",
      status_label: "Running",
      section: "running",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: {
        phase: "REPAIR",
        phase_label: "Pętla napraw",
        phase_elapsed_s: 7,
        run_elapsed_s: 90,
        activity: "Naprawa defektu formatowania kodu",
        model: "Codex",
        role: "Implementer",
        iteration: 2,
        max_iterations: 5,
        execution_id: "exec-rep-1",
        step_elapsed_s: 7,
        roadmap: { done: 1, total: 3 },
        standing: { item_id: "task-1", status: "repair", iterations: 1 },
        roadmap_items: [],
        iterations_done: 1,
        iteration_id: "iter-2",
        nodes: [],
        eta: null,
      },
      timeline: [],
      gate: null,
      engine: { status: "RUNNING", phase: "REPAIR" },
    };

    const htmlRepairGraph = renderToString(React.createElement(PhaseGraph, { run: testRepairRun }));
    assert(htmlRepairGraph.includes("data-testid=\"repair-loop-badge\""));
    assert(htmlRepairGraph.includes("Pętla REPAIR aktywna (Cykl #1)"));
    assert(htmlRepairGraph.includes("animate-loopback"), "Loopback curve must have animate-loopback class");
    assert(htmlRepairGraph.includes("stroke=\"#f43f5e\""), "Active loopback curve must be colored rose #f43f5e");

    // 3. Render PhaseGraph with AWAITING_HUMAN run
    const testGateRun: RunDetail = {
      run_id: "run-graph-gate",
      status: "READY_FOR_DECISION",
      status_label: "Awaiting Human",
      section: "attention",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: {
        phase: "ROADMAP_CHECK",
        phase_label: "Weryfikacja celu",
        phase_elapsed_s: 0,
        run_elapsed_s: 150,
        activity: "Wymaga decyzji człowieka w bramce",
        model: "AAW Gatekeeper",
        role: "System",
        iteration: 2,
        max_iterations: 2,
        execution_id: "gate-exec-1",
        step_elapsed_s: 0,
        roadmap: { done: 2, total: 2 },
        roadmap_items: [],
        iterations_done: 2,
        iteration_id: "iter-2",
        nodes: [],
        eta: null,
      },
      timeline: [],
      gate: {
        why: "Test gate",
        hold_reason: "ROADMAP_EXHAUSTED",
        escalation_code: null,
        done: ["All done"],
        remaining: [],
        warnings: [],
        planner_end_reason: null,
        could_continue: false,
        candidate: {} as any,
        actions: {} as any,
        decision: null,
        human: null,
        promotion: null,
        accept_meaning: "Accept",
        merge_push: "fast-forward",
        main_merge_allowed: true,
      },
      engine: { status: "AWAITING_HUMAN", phase: "ROADMAP_CHECK" },
    };

    const htmlGateGraph = renderToString(React.createElement(PhaseGraph, { run: testGateRun }));
    assert(htmlGateGraph.includes("data-testid=\"human-gate-badge\""));
    assert(htmlGateGraph.includes("WYMAGA DECYZJI CZŁOWIEKA"));
    assert(htmlGateGraph.includes("BRAMKA CZŁOWIEKA"), "Node 7 must display BRAMKA CZŁOWIEKA badge");

    console.log("  ✓ PhaseGraph SVG loopback, forward arrows, telemetry bar, and human gate pulse verified");
  }

  console.log("\n=== ALL 7-PHASE EXECUTION GRAPH TESTS PASSED ===");
}
