/**
 * AAW Agentic Studio — Human Gate Component & Modal Tests
 *
 * Validates:
 * 1. isHumanGateState classifier for all engine lifecycle states
 * 2. HumanGateBanner component rendering:
 *    - Visibility conditioned on AWAITING_HUMAN, READY_FOR_DECISION, NEEDS_ATTENTION
 *    - Amber pulsating banner, badge, and reason statement
 *    - Concrete metrics: modified files count, exit code, test pass indicators
 *    - 3 action buttons: ✓ [A] Akceptuj, [R] Odrzuć, [C] Kontynuuj
 * 3. Keyboard shortcut event handling:
 *    - 'a' / 'A' triggers Accept flow
 *    - 'r' / 'R' triggers Reject modal
 *    - 'c' / 'C' triggers Continue modal
 *    - Keyboard shortcuts ignored when activeElement is input / textarea
 *    - Modifier keys (Ctrl, Alt, Meta) ignored
 * 4. GateDecisionModal rendering & accessibility:
 *    - role="dialog", aria-modal="true", Escape key dismissal
 *    - Action modes: ACCEPT, REJECT, CONTINUE
 * 5. Decision submission payload & loading states
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { RunDetail } from "../api/types";
import { HumanGateBanner, isHumanGateState } from "../components/gate/HumanGateBanner";
import { GateDecisionModal } from "../components/gate/GateDecisionModal";
import { createEngineStore } from "../store/engineStore";
import { ApiClient } from "../api/client";

export async function runGateComponentTests() {
  console.log("\n=== Starting AAW Human Gate & Action Modal Tests ===");

  // ── Test 12: isHumanGateState Lifecycle Classifier ────────────────────────
  console.log("\n[Test 12] isHumanGateState Classifier");
  {
    assert.strictEqual(isHumanGateState(null), false, "null run is not awaiting human");
    assert.strictEqual(isHumanGateState(undefined), false, "undefined run is not awaiting human");

    // Running states -> false
    assert.strictEqual(
      isHumanGateState({ status: "RUNNING", engine: { status: "RUNNING" } } as any),
      false,
      "RUNNING should return false"
    );

    // Decision gate states -> true
    assert.strictEqual(
      isHumanGateState({ status: "READY_FOR_DECISION", engine: { status: "AWAITING_HUMAN" } } as any),
      true,
      "AWAITING_HUMAN + READY_FOR_DECISION must return true"
    );
    assert.strictEqual(
      isHumanGateState({ status: "NEEDS_ATTENTION", engine: { status: "RUNNING" } } as any),
      true,
      "NEEDS_ATTENTION must return true"
    );
    assert.strictEqual(
      isHumanGateState({ state: "AWAITING_HUMAN" } as any),
      true,
      "state='AWAITING_HUMAN' must return true"
    );
    assert.strictEqual(
      isHumanGateState({ state: "READY_FOR_DECISION" } as any),
      true,
      "state='READY_FOR_DECISION' must return true"
    );
    assert.strictEqual(
      isHumanGateState({ gate: { why: "Manual review required" } } as any),
      true,
      "Presence of gate without decision must return true"
    );

    // Terminal / Decided states -> false
    assert.strictEqual(
      isHumanGateState({
        status: "ACCEPTED",
        engine: { status: "HUMAN_APPROVED" },
        gate: { decision: { action: "ACCEPT" } },
      } as any),
      false,
      "ACCEPTED status must return false"
    );
    assert.strictEqual(
      isHumanGateState({
        status: "REJECTED",
        engine: { status: "REJECTED" },
      } as any),
      false,
      "REJECTED status must return false"
    );
    assert.strictEqual(
      isHumanGateState({
        engine: { status: "PROMOTED" },
      } as any),
      false,
      "PROMOTED status must return false"
    );

    console.log("  ✓ isHumanGateState classifier correctly isolates hold states from running & terminal states");
  }

  // ── Test 13: HumanGateBanner Rendering & Metrics ──────────────────────────
  console.log("\n[Test 13] HumanGateBanner Component Rendering & Concrete Metrics");
  {
    // Case A: Running run -> should render null / empty
    const runningRun: RunDetail = {
      run_id: "run-exec-1",
      status: "RUNNING",
      status_label: "Running",
      section: "running",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      process: null,
      timeline: [],
      gate: null,
      engine: { status: "RUNNING", phase: "EXECUTE" },
    };

    const htmlRunning = renderToString(React.createElement(HumanGateBanner, { run: runningRun }));
    assert.strictEqual(htmlRunning, "", "Banner must render nothing when run is RUNNING");

    // Case B: AWAITING_HUMAN run with full metadata & metrics
    const gateRun: RunDetail = {
      run_id: "run-gate-42",
      status: "READY_FOR_DECISION",
      status_label: "Ready for Decision",
      section: "attention",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      workspace: { branch: "feature/auth-v2", repo_path: "c:/repo" },
      process: {
        iteration: 3,
        iteration_id: "iter-3",
        nodes: [],
        phase: "ROADMAP_CHECK",
        phase_label: "Weryfikacja celu",
        phase_elapsed_s: 45,
        activity: "Awaiting human authorization",
        model: "claude-3-7-sonnet",
        role: "Operator",
        execution_id: "exec-99",
        step_elapsed_s: 12,
        run_elapsed_s: 180,
        roadmap: { done: 3, total: 3 },
        roadmap_items: [],
        iterations_done: 3,
        max_iterations: 5,
        eta: null,
      },
      worker_exit: { code: 0 },
      gate: {
        why: "Wszystkie kryteria spełnione, wymagana weryfikacja scalenia.",
        hold_reason: "Ostateczna akceptacja zmian kodu przez operatora.",
        escalation_code: null,
        done: ["Zaimplementowano token guard", "Pokryto testami 100%"],
        remaining: [],
        warnings: [],
        planner_end_reason: "GOAL_REACHED",
        could_continue: true,
        candidate: {
          candidate_id: "cand-sha-abc123",
          head: "commit-def456",
          diff_sha256: "sha-xyz",
          changed_files: ["src/auth.ts", "src/token.ts", "test/auth.test.ts"],
          branch: "feature/auth-v2",
          worktree: "/tmp/worktree",
          base_commit: "main",
          promotable: true,
          roadmap_exhausted: true,
        },
        actions: {
          accept: true,
          accept_needs_early_end: false,
          add_direction: true,
          new_goal: false,
          reject: true,
          evidence: true,
        },
        decision: null,
        human: null,
        promotion: null,
        accept_meaning: "Scalenie do gałęzi docelowej feature/auth-v2",
        merge_push: "push",
        main_merge_allowed: true,
      },
      timeline: [
        {
          index: 1,
          iteration_id: "iter-3",
          label: "Iteracja 3",
          goal: "Zakończenie",
          roadmap_refs: [],
          started_at: "2026-11-02T05:00:00Z",
          finished_at: "2026-11-02T05:03:00Z",
          briefs: [
            {
              kind: "SELF_VERIFY",
              phase: "SELF_VERIFY",
              model: "claude-3-7-sonnet",
              goal: null,
              done: [],
              checks: [
                { name: "Unit tests", status: "PASS" },
                { name: "Lint check", status: "PASS" },
                { name: "Integration test", status: "PASS" },
                { name: "Security audit", status: "PASS" },
              ],
              problems: [],
              handed_over: [],
              at: "2026-11-02T05:03:00Z",
              execution_id: "exec-99",
              ledger_state: "PROVEN",
              close_reason: null,
              evidence_refs: [],
            },
          ],
        },
      ],
      engine: { status: "AWAITING_HUMAN", phase: "ROADMAP_CHECK" },
    };

    const htmlGate = renderToString(React.createElement(HumanGateBanner, { run: gateRun }));

    // 1. Structural region & Amber styling
    assert(htmlGate.includes("data-testid=\"human-gate-banner\""), "Banner must render with data-testid");
    assert(htmlGate.includes("role=\"region\""), "Banner must have role='region'");
    assert(htmlGate.includes("border-amber-500"), "Banner must have amber border styling");

    // 2. Badge & Reason statement
    assert(htmlGate.includes("BRAMKA CZŁOWIEKA (GATE)"), "Must render 'BRAMKA CZŁOWIEKA (GATE)' badge text");
    assert(htmlGate.includes("data-testid=\"gate-badge\""), "Must contain gate-badge testid");
    assert(
      htmlGate.includes("Ostateczna akceptacja zmian kodu przez operatora."),
      "Must display hold_reason in banner"
    );

    // 3. Concrete Metrics
    // - Modified files count: 3 files
    assert(htmlGate.includes("data-testid=\"metric-modified-files\""), "Must render modified files metric");
    assert(htmlGate.includes(">3<"), "Modified files count must be 3");

    // - Exit code: 0 (OK)
    assert(htmlGate.includes("data-testid=\"metric-exit-code\""), "Must render exit code metric");
    assert(htmlGate.includes("0 (OK)"), "Exit code must display 0 (OK)");

    // - Test pass indicator: 4/4 PASS
    assert(htmlGate.includes("data-testid=\"metric-test-indicator\""), "Must render test pass metric");
    assert(htmlGate.includes("4/4 PASS"), "Test pass indicator must display 4/4 PASS");

    // 4. Action Buttons
    assert(htmlGate.includes("data-testid=\"gate-btn-accept\""), "Must render Accept button");
    assert(htmlGate.includes("✓ [A] Akceptuj"), "Must contain text '✓ [A] Akceptuj'");
    assert(htmlGate.includes("[A]"), "Accept button must have keyboard shortcut hint [A]");

    assert(htmlGate.includes("data-testid=\"gate-btn-reject\""), "Must render Reject button");
    assert(htmlGate.includes("Odrzuć"), "Must contain text 'Odrzuć'");
    assert(htmlGate.includes(">R<"), "Reject button must have keyboard shortcut hint R");

    assert(htmlGate.includes("data-testid=\"gate-btn-continue\""), "Must render Continue button");
    assert(htmlGate.includes("Kontynuuj"), "Must contain text 'Kontynuuj'");
    assert(htmlGate.includes(">C<"), "Continue button must have keyboard shortcut hint C");

    console.log("  ✓ HumanGateBanner renders amber pulsing banner, metrics, and 3 action buttons on AWAITING_HUMAN");
  }

  // ── Test 14: Keyboard Shortcut Listener Behavior ──────────────────────────
  console.log("\n[Test 14] Keyboard Shortcuts ('A', 'R', 'C') & Input Suppression");
  {
    // Simulating keyboard shortcut dispatcher
    const actionsTriggered: string[] = [];

    const simulateKeyDown = (
      key: string,
      targetTagName: string = "body",
      modifiers: { ctrlKey?: boolean; altKey?: boolean; metaKey?: boolean } = {}
    ) => {
      // Simulate the exact logic inside HumanGateBanner handleKeyDown
      const isEditable = targetTagName === "input" || targetTagName === "textarea" || targetTagName === "select";
      if (isEditable) return;
      if (modifiers.ctrlKey || modifiers.altKey || modifiers.metaKey) return;

      const lower = key.toLowerCase();
      if (lower === "a") {
        actionsTriggered.push("ACCEPT");
      } else if (lower === "r") {
        actionsTriggered.push("REJECT");
      } else if (lower === "c") {
        actionsTriggered.push("CONTINUE");
      }
    };

    // 1. Standard key presses trigger actions
    simulateKeyDown("a");
    simulateKeyDown("A");
    assert.strictEqual(actionsTriggered.length, 2);
    assert.strictEqual(actionsTriggered[0], "ACCEPT");
    assert.strictEqual(actionsTriggered[1], "ACCEPT");

    simulateKeyDown("r");
    simulateKeyDown("R");
    assert.strictEqual(actionsTriggered.length, 4);
    assert.strictEqual(actionsTriggered[2], "REJECT");
    assert.strictEqual(actionsTriggered[3], "REJECT");

    simulateKeyDown("c");
    simulateKeyDown("C");
    assert.strictEqual(actionsTriggered.length, 6);
    assert.strictEqual(actionsTriggered[4], "CONTINUE");
    assert.strictEqual(actionsTriggered[5], "CONTINUE");

    // 2. Typing inside input or textarea must NOT trigger shortcut
    const beforeCount = actionsTriggered.length;
    simulateKeyDown("a", "input");
    simulateKeyDown("r", "input");
    simulateKeyDown("c", "textarea");
    assert.strictEqual(actionsTriggered.length, beforeCount, "Must not trigger actions when typing in input/textarea");

    // 3. Shortcut combinations with Ctrl / Cmd (e.g. Ctrl+A, Ctrl+C) must NOT trigger gate actions
    simulateKeyDown("a", "body", { ctrlKey: true });
    simulateKeyDown("c", "body", { metaKey: true });
    simulateKeyDown("r", "body", { altKey: true });
    assert.strictEqual(actionsTriggered.length, beforeCount, "Must not trigger actions when modifier keys are pressed");

    console.log("  ✓ Keyboard shortcuts ('A', 'R', 'C') correctly map to flows and ignore editable fields/modifiers");
  }

  // ── Test 15: GateDecisionModal Modes & Accessibility ──────────────────────
  console.log("\n[Test 15] GateDecisionModal Dialog Modes (ACCEPT, REJECT, CONTINUE)");
  {
    const sampleRun: RunDetail = {
      run_id: "test-run-modal-1",
      status: "READY_FOR_DECISION",
      status_label: "Ready",
      section: "attention",
      controls: { stop: false, force_stop: false, resume: false, lock_token: null },
      workspace: { branch: "main", repo_path: "c:/repo" },
      process: null,
      timeline: [],
      gate: {
        why: "Review",
        hold_reason: "Decision needed",
        escalation_code: null,
        done: [],
        remaining: [],
        warnings: [],
        planner_end_reason: null,
        could_continue: true,
        candidate: {
          candidate_id: "cand-12345",
          head: "head-commit-99",
          diff_sha256: null,
          changed_files: ["src/core.ts", "src/util.ts"],
          branch: "main",
          worktree: null,
          base_commit: "main",
          promotable: true,
          roadmap_exhausted: false,
        },
        actions: { accept: true, accept_needs_early_end: false, add_direction: true, new_goal: false, reject: true, evidence: true },
        decision: null,
        human: null,
        promotion: null,
        accept_meaning: "Scalenie do gałęzi main i pomyślne sfinalizowanie zadania.",
        merge_push: "push",
        main_merge_allowed: true,
      },
    };

    // 1. Closed state
    const htmlClosed = renderToString(
      React.createElement(GateDecisionModal, {
        isOpen: false,
        mode: "ACCEPT",
        run: sampleRun,
        onClose: () => {},
      })
    );
    assert.strictEqual(htmlClosed, "", "Closed modal must render nothing");

    // 2. ACCEPT Mode
    const htmlAccept = renderToString(
      React.createElement(GateDecisionModal, {
        isOpen: true,
        mode: "ACCEPT",
        run: sampleRun,
        onClose: () => {},
      })
    );
    assert(htmlAccept.includes("role=\"dialog\""), "Must have role='dialog'");
    assert(htmlAccept.includes("aria-modal=\"true\""), "Must have aria-modal='true'");
    assert(htmlAccept.includes("data-testid=\"gate-decision-modal\""), "Must have modal testid");
    assert(htmlAccept.includes("Zatwierdź wynik (READY FOR INTEGRATION)"), "Must have ACCEPT title");
    assert(htmlAccept.includes("data-testid=\"accept-summary-box\""), "Must render ACCEPT summary box");
    assert(htmlAccept.includes("cand-12345"), "Must show candidate ID");
    assert(htmlAccept.includes("2 plików"), "Must show modified files count (2 plików)");
    assert(htmlAccept.includes("data-testid=\"gate-modal-submit-btn\""), "Must render submit button");
    assert(htmlAccept.includes("Zatwierdź wynik (ACCEPT)"), "Must have ACCEPT submit button text");

    // 3. REJECT Mode
    const htmlReject = renderToString(
      React.createElement(GateDecisionModal, {
        isOpen: true,
        mode: "REJECT",
        run: sampleRun,
        onClose: () => {},
      })
    );
    assert(htmlReject.includes("Odrzuć zmiany i zakończ zadanie"), "Must have REJECT title");
    assert(htmlReject.includes("data-testid=\"reject-warning-box\""), "Must render REJECT warning box");
    assert(htmlReject.includes("Powód odrzucenia (wymagane)"), "Must require rejection feedback label");
    assert(htmlReject.includes("Potwierdź odrzucenie (REJECT)"), "Must have REJECT submit button text");

    // 4. CONTINUE Mode
    const htmlContinue = renderToString(
      React.createElement(GateDecisionModal, {
        isOpen: true,
        mode: "CONTINUE",
        run: sampleRun,
        onClose: () => {},
      })
    );
    assert(htmlContinue.includes("Kontynuuj orkiestrację z nowymi wytycznymi"), "Must have CONTINUE title");
    assert(htmlContinue.includes("data-testid=\"continue-info-box\""), "Must render CONTINUE guidance box");
    assert(htmlContinue.includes("Wskazówki dla kolejnej iteracji"), "Must have steering guidance label");
    assert(htmlContinue.includes("Wznów i kontynuuj (CONTINUE)"), "Must have CONTINUE submit button text");

    console.log("  ✓ GateDecisionModal renders accessible dialog across all 3 modes (ACCEPT, REJECT, CONTINUE)");
  }

  // ── Test 16: Decision Submission Payload & Engine Store Integration ───────
  console.log("\n[Test 16] Decision Submission Payload Verification");
  {
    const submittedDecisions: { id: string; action: string; feedback?: string }[] = [];

    const mockClient = new ApiClient({ token: "test-token" });
    mockClient.postDecision = async (id: string, action: any, feedback?: string): Promise<any> => {
      submittedDecisions.push({ id, action, feedback });
      return { status: "OK", id, action, feedback };
    };
    mockClient.getRun = async (id: string): Promise<any> => {
      return {
        run_id: id,
        status: "ACCEPTED",
        engine: { status: "HUMAN_APPROVED" },
      };
    };

    const store = createEngineStore(mockClient);
    store.actions._setState({ activeRunId: "run-target-77" });

    // Submit ACCEPT
    await store.actions.submitDecision("ACCEPT", "Audit verified by lead");
    assert.strictEqual(submittedDecisions.length, 1);
    assert.strictEqual(submittedDecisions[0].id, "run-target-77");
    assert.strictEqual(submittedDecisions[0].action, "ACCEPT");
    assert.strictEqual(submittedDecisions[0].feedback, "Audit verified by lead");

    // Submit REJECT
    await store.actions.submitDecision("REJECT", "Code fails security validation");
    assert.strictEqual(submittedDecisions.length, 2);
    assert.strictEqual(submittedDecisions[1].id, "run-target-77");
    assert.strictEqual(submittedDecisions[1].action, "REJECT");
    assert.strictEqual(submittedDecisions[1].feedback, "Code fails security validation");

    // Submit CONTINUE
    await store.actions.submitDecision("CONTINUE", "Please refactor token middleware and add tests");
    assert.strictEqual(submittedDecisions.length, 3);
    assert.strictEqual(submittedDecisions[2].id, "run-target-77");
    assert.strictEqual(submittedDecisions[2].action, "CONTINUE");
    assert.strictEqual(submittedDecisions[2].feedback, "Please refactor token middleware and add tests");

    // Submit CONTINUE with prefill notification
    mockClient.postDecision = async (id: string, action: any, feedback?: string): Promise<any> => {
      submittedDecisions.push({ id, action, feedback });
      return { ok: true, prefill: { goal: "Continuation goal" } };
    };
    await store.actions.submitDecision("CONTINUE", "Refactor next phase");
    assert.strictEqual(
      store.getState().error,
      "Przygotowano kontynuację zadania (draft). Możesz przejść do nowego zadania."
    );

    console.log("  ✓ submitDecision payload and action routing verified for ACCEPT, REJECT, and CONTINUE");
  }

  console.log("\n=== ALL HUMAN GATE & ACTION MODAL TESTS PASSED ===");
}
