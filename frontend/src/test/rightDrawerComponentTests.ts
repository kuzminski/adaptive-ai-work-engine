/**
 * AAW Agentic Studio — Right Drawer, Telemetry Terminal, Diff & Evidence Tests
 *
 * Validates:
 * 1. TelemetryTerminal:
 *    - Event formatting from boot, run.events, timeline briefs, and journal
 *    - Role badge mapping (Planner, Implementer, Verifier, System)
 *    - Status coloring (passed: green, failed: red, gate: amber)
 *    - Role filtering pills (All / Wszystkie, Planner, Implementer, Verifier, System)
 *    - Auto-scroll control and line count footer
 * 2. UnifiedDiffViewer:
 *    - Unified diff parser for multi-file patches
 *    - Line-by-line syntax classifications (additions, deletions, chunks, headers)
 *    - Multi-file selector chips and active file view filtering
 *    - Truncation banner for diffs > 200 KB or flagged truncated
 *    - Empty diff fallback state
 * 3. EvidenceLedger:
 *    - Test Proof card with pytest exit code 0
 *    - Linter Proof card with ruff clean checks
 *    - Changed Files Audit card with candidate ID and file list
 *    - Safety Invariants card (Human Gate, Loopback isolation, Zero runtime Python dependencies)
 * 4. RightDrawer:
 *    - Tab navigation (Terminal & Logi, Unified Diff, Księga Dowodów)
 *    - Active tab rendering and diff count chip
 *    - Width toggle (380px to 440px) and close button
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { EvidenceView, RunDetail } from "../api/types";
import {
  TelemetryTerminal,
  formatTelemetryLogs,
} from "../components/terminal/TelemetryTerminal";
import {
  UnifiedDiffViewer,
  parseUnifiedDiff,
} from "../components/diff/UnifiedDiffViewer";
import { EvidenceLedger } from "../components/evidence/EvidenceLedger";
import { RightDrawer } from "../components/drawer/RightDrawer";

export async function runRightDrawerComponentTests() {
  console.log("\n=== Starting AAW Right Drawer & Telemetry Console Tests ===");

  // Mock Run and Evidence
  const sampleRun: RunDetail = {
    run_id: "run-20261102-test",
    status: "READY_FOR_DECISION",
    status_label: "Awaiting Human Approval",
    section: "attention",
    project: "AAW Studio Telemetry Test",
    goal: "Verify right drawer components end-to-end",
    created_at: "2026-11-02T04:30:00Z",
    controls: {
      stop: false,
      force_stop: false,
      resume: false,
      lock_token: null,
    },
    engine: {
      status: "AWAITING_HUMAN",
      phase: "ROADMAP_CHECK",
    },
    process: {
      iteration: 2,
      iteration_id: "iter-02",
      nodes: [],
      phase: "ROADMAP_CHECK",
      phase_label: "Roadmap Check",
      phase_elapsed_s: 4.2,
      activity: "Waiting for human gate decision",
      model: "claude-3-7-sonnet",
      role: "System",
      execution_id: "exec-99",
      step_elapsed_s: 1.1,
      run_elapsed_s: 45.8,
      roadmap: { done: 3, total: 3 },
      roadmap_items: [],
      iterations_done: 2,
      max_iterations: 5,
      eta: null,
    },
    timeline: [
      {
        index: 1,
        iteration_id: "iter-01",
        label: "Phase 1 - Planning",
        goal: "Analyze repository and plan architecture",
        roadmap_refs: ["item-1"],
        started_at: "2026-11-02T04:30:05Z",
        finished_at: "2026-11-02T04:30:20Z",
        briefs: [
          {
            kind: "plan",
            phase: "PLAN",
            model: "claude-3-7-sonnet",
            goal: "Produce implementation plan",
            done: ["Plan created in .superpowers/"],
            checks: [],
            problems: [],
            handed_over: ["Handed off to implementer"],
            at: "2026-11-02T04:30:15Z",
            execution_id: "exec-01",
            ledger_state: "OK",
            close_reason: "PLAN_COMPLETE",
            evidence_refs: [],
          },
        ],
      },
      {
        index: 2,
        iteration_id: "iter-02",
        label: "Phase 2 - Execution & Verification",
        goal: "Implement right drawer components and run tests",
        roadmap_refs: ["item-2"],
        started_at: "2026-11-02T04:30:25Z",
        finished_at: "2026-11-02T04:30:45Z",
        briefs: [
          {
            kind: "implement",
            phase: "EXECUTE",
            model: "claude-3-7-sonnet",
            goal: "Create TelemetryTerminal, UnifiedDiffViewer, EvidenceLedger",
            done: ["All 3 components written cleanly"],
            checks: [
              {
                name: "pytest",
                status: "PASS",
                summary: "5 passed in 2.55s, exit code 0",
              },
              {
                name: "ruff",
                status: "PASS",
                summary: "All checks passed, 0 errors",
              },
            ],
            problems: [],
            handed_over: [],
            at: "2026-11-02T04:30:40Z",
            execution_id: "exec-02",
            ledger_state: "OK",
            close_reason: "EXECUTE_SUCCESS",
            evidence_refs: [],
          },
        ],
      },
    ],
    gate: {
      why: "Iteracja zakończona sukcesem — wymagane potwierdzenie przed mergem do main",
      hold_reason: "Plan wykonany w 100%, wszystkie testy zielone",
      escalation_code: null,
      done: ["Zaimplementowano RightDrawer", "Zaimplementowano TelemetryTerminal"],
      remaining: [],
      warnings: [],
      planner_end_reason: "TASK_DONE",
      could_continue: false,
      candidate: {
        candidate_id: "cand-20261102-aaw",
        head: "8a4f09c2e",
        diff_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        changed_files: [
          "frontend/src/components/drawer/RightDrawer.tsx",
          "frontend/src/components/terminal/TelemetryTerminal.tsx",
        ],
        branch: "feature/right-drawer",
        worktree: "c:/adaptive-ai-work-engine",
        base_commit: "1b2c3d4e",
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
      accept_meaning: "Scal zmiany z gałęzią główną i zakończ przebieg",
      merge_push: "PRODUCE_DIFF",
      main_merge_allowed: true,
    },
  };

  const sampleEvidence: EvidenceView = {
    run_dir: "c:/adaptive-ai-work-engine/.aaw/runs/run-20261102-test",
    latest_diff: `diff --git a/frontend/src/components/drawer/RightDrawer.tsx b/frontend/src/components/drawer/RightDrawer.tsx
index 1111111..2222222 100644
--- a/frontend/src/components/drawer/RightDrawer.tsx
+++ b/frontend/src/components/drawer/RightDrawer.tsx
@@ -1,5 +1,8 @@
+import React from "react";
+import { Terminal } from "lucide-react";
-export function OldDrawer() {}
+export function RightDrawer() {
+  return <div>New Drawer</div>;
+}
diff --git a/frontend/src/components/terminal/TelemetryTerminal.tsx b/frontend/src/components/terminal/TelemetryTerminal.tsx
index 3333333..4444444 100644
--- a/frontend/src/components/terminal/TelemetryTerminal.tsx
+++ b/frontend/src/components/terminal/TelemetryTerminal.tsx
@@ -10,3 +10,4 @@
 export function TelemetryTerminal() {}
+// End of file
`,
    journal: [
      {
        event_type: "phase_transition",
        phase: "EXECUTE",
        occurred_at: "2026-11-02T04:30:26Z",
        payload: { message: "Rozpoczęto fazę EXECUTE" },
      },
    ],
  };

  // ── Test 17: TelemetryTerminal log formatting, roles, and status colors ──
  console.log("\n[Test 17] TelemetryTerminal Log Formatting & Roles");
  {
    const logs = formatTelemetryLogs(sampleRun, sampleEvidence);
    assert(logs.length >= 8, `Expected at least 8 logs, got ${logs.length}`);

    // Check baseline system logs
    const bootLogs = logs.filter((l) => l.id.startsWith("boot-"));
    assert.strictEqual(bootLogs.length, 3, "Must have 3 boot entries");
    assert.strictEqual(bootLogs[0].role, "System");
    assert.strictEqual(bootLogs[0].timestamp, "[--:--:--]", "Boot log 0 must use synthetic [--:--:--]");
    assert.strictEqual(bootLogs[1].role, "Planner");
    assert.strictEqual(bootLogs[1].timestamp, "[--:--:--]", "Boot log 1 must use synthetic [--:--:--]");
    assert.strictEqual(bootLogs[2].role, "System");
    assert.strictEqual(bootLogs[2].timestamp, "[--:--:--]", "Boot log 2 must use synthetic [--:--:--]");

    // Check roles representation
    const rolesPresent = new Set(logs.map((l) => l.role));
    assert(rolesPresent.has("System"), "Must contain System logs");
    assert(rolesPresent.has("Planner"), "Must contain Planner logs");
    assert(rolesPresent.has("Implementer"), "Must contain Implementer logs");

    // Check status colors
    const hasPassed = logs.some((l) => l.status === "passed");
    const hasGate = logs.some((l) => l.status === "gate");
    assert(hasPassed, "Must have passed status items (e.g. pytest check / boot)");
    assert(hasGate, "Must have gate status items (Human gate hold)");

    // Test component rendering
    const html = renderToString(
      React.createElement(TelemetryTerminal, {
        run: sampleRun,
        evidence: sampleEvidence,
        roleFilter: "All",
      })
    );

    assert(html.includes("data-testid=\"terminal-viewport\""), "Must render terminal viewport");
    assert(html.includes("data-testid=\"role-filter-All\""), "Must render All role pill");
    assert(html.includes("data-testid=\"role-filter-Planner\""), "Must render Planner role pill");
    assert(html.includes("data-testid=\"role-filter-Implementer\""), "Must render Implementer role pill");
    assert(html.includes("data-testid=\"role-filter-Verifier\""), "Must render Verifier role pill");
    assert(html.includes("data-testid=\"role-filter-System\""), "Must render System role pill");
    assert(html.includes("data-testid=\"terminal-scroll-bottom\""), "Must render scroll-to-bottom button");
    assert(html.includes("Połączono z lokalnym silnikiem loopback"), "Must include baseline connection log");
    assert(html.includes("BRAMKA HUMAN-IN-LOOP"), "Must include gate log");

    // Test filtered role rendering
    const plannerHtml = renderToString(
      React.createElement(TelemetryTerminal, {
        run: sampleRun,
        evidence: sampleEvidence,
        roleFilter: "Planner",
      })
    );
    assert(plannerHtml.includes("Katalog modeli zweryfikowany"), "Planner log must be present");
    assert(!plannerHtml.includes("Połączono z lokalnym silnikiem"), "System log must be excluded when filtering by Planner");

    console.log("  ✓ TelemetryTerminal formatting, role badges, colorizing, and filtering verified");
  }

  // ── Test 18: UnifiedDiffViewer diff parser, colors, and multi-file chips ─
  console.log("\n[Test 18] UnifiedDiffViewer Multi-File Parsing & Syntax");
  {
    const files = parseUnifiedDiff(sampleEvidence.latest_diff || "");
    assert.strictEqual(files.length, 2, "Must parse exactly 2 changed files");

    // File 1 verification
    const file1 = files[0];
    assert.strictEqual(file1.fileName, "RightDrawer.tsx");
    assert(file1.additions > 0, "Must have additions");
    assert(file1.deletions > 0, "Must have deletions");

    const addLines = file1.lines.filter((l) => l.type === "add");
    const delLines = file1.lines.filter((l) => l.type === "delete");
    const chunkLines = file1.lines.filter((l) => l.type === "chunk");
    assert(addLines.length > 0, "Must identify addition lines");
    assert(delLines.length > 0, "Must identify deletion lines");
    assert(chunkLines.length > 0, "Must identify chunk headers");

    // File 2 verification
    const file2 = files[1];
    assert.strictEqual(file2.fileName, "TelemetryTerminal.tsx");

    // Render component
    const html = renderToString(
      React.createElement(UnifiedDiffViewer, {
        run: sampleRun,
        evidence: sampleEvidence,
      })
    );

    assert(html.includes("data-testid=\"diff-viewport\""), "Must render diff viewport");
    assert(html.includes("data-testid=\"diff-file-chip-all\""), "Must render 'All' files chip");
    assert(html.includes("data-testid=\"diff-file-chip-0\""), "Must render file chip 0");
    assert(html.includes("data-testid=\"diff-file-chip-1\""), "Must render file chip 1");
    assert(html.includes("data-testid=\"diff-copy-button\""), "Must render copy diff button");
    assert(html.includes("data-testid=\"diff-line-add\""), "Must render addition lines");
    assert(html.includes("data-testid=\"diff-line-delete\""), "Must render deletion lines");
    assert(html.includes("data-testid=\"diff-line-chunk\""), "Must render chunk header lines");

    // Empty state test
    const emptyHtml = renderToString(
      React.createElement(UnifiedDiffViewer, {
        run: null,
        evidence: null,
      })
    );
    assert(emptyHtml.includes("data-testid=\"diff-empty-state\""), "Must render empty state when no diff");

    // Truncation banner test (> 200 KB)
    const largeDiff = "diff --git a/test.txt b/test.txt\n" + "+line\n".repeat(45000);
    const truncEvidence: EvidenceView = {
      latest_diff: largeDiff,
    };
    const truncHtml = renderToString(
      React.createElement(UnifiedDiffViewer, {
        run: sampleRun,
        evidence: truncEvidence,
      })
    );
    assert(truncHtml.includes("data-testid=\"diff-truncation-banner\""), "Must display truncation banner for diff > 200 KB");

    console.log("  ✓ UnifiedDiffViewer multi-file parsing, chips, color syntax, and truncation banner verified");
  }

  // ── Test 19: EvidenceLedger proof cards and safety invariants ─────────────
  console.log("\n[Test 19] EvidenceLedger Proof Cards & Invariants");
  {
    const html = renderToString(
      React.createElement(EvidenceLedger, {
        run: sampleRun,
        evidence: sampleEvidence,
      })
    );

    // Container
    assert(html.includes("data-testid=\"evidence-ledger-container\""), "Must render ledger container");

    // Card 1: Test Proof
    assert(html.includes("data-testid=\"evidence-card-test-proof\""), "Must render Test Proof card");
    assert(html.includes("Dowód Testów (Pytest Proof)"), "Must include Test Proof header");
    assert(html.includes("PASS (Exit Code 0)"), "Must display exit code 0 PASS badge");
    assert(html.includes("5 passed in 2.55s"), "Must display test suite results summary");

    // Card 2: Linter Proof
    assert(html.includes("data-testid=\"evidence-card-linter-proof\""), "Must render Linter Proof card");
    assert(html.includes("Dowód Jakości Kodu (Ruff Linter Proof)"), "Must include Linter Proof header");
    assert(html.includes("PASS (0 Naruszeń)"), "Must display 0 violations badge");

    // Card 3: Changed Files Audit
    assert(html.includes("data-testid=\"evidence-card-changed-files\""), "Must render Changed Files Audit card");
    assert(html.includes("data-testid=\"changed-files-count-badge\""), "Must render changed files badge");
    assert(html.includes("2 pliki"), "Must count 2 modified files");
    assert(html.includes("cand-20261102-aaw"), "Must include candidate ID");
    assert(html.includes("data-testid=\"evidence-changed-file-0\""), "Must render file row 0");
    assert(html.includes("data-testid=\"evidence-changed-file-1\""), "Must render file row 1");

    // Card 4: Safety Invariants
    assert(html.includes("data-testid=\"evidence-card-safety-invariants\""), "Must render Safety Invariants card");
    assert(html.includes("data-testid=\"invariant-human-gate\""), "Must render Human Gate invariant");
    assert(html.includes("data-testid=\"invariant-loopback\""), "Must render Loopback security invariant");
    assert(html.includes("data-testid=\"invariant-zero-deps\""), "Must render Zero Dependencies invariant");
    assert(html.includes("3/3 ENFORCED"), "Must show 3/3 active invariants badge");

    // Test 19b: EvidenceLedger neutral state when no pytest/ruff checks are recorded
    const emptyRunHtml = renderToString(
      React.createElement(EvidenceLedger, {
        run: { run_id: "empty-run", status: "PENDING" } as any,
        evidence: null,
      })
    );
    assert(emptyRunHtml.includes("BRAK DANYCH"), "Must render BRAK DANYCH badge for missing checks");
    assert(emptyRunHtml.includes("Brak zarejestrowanych danych testowych dla tego przebiegu"), "Must display neutral test summary");
    assert(emptyRunHtml.includes("Brak zarejestrowanych wyników lintera"), "Must display neutral linter summary");
    assert(!emptyRunHtml.includes("python -m pytest tests/test_product_server_static.py"), "Must not contain fake pytest command");
    assert(!emptyRunHtml.includes("Python 3.14 (Clean Sandbox)"), "Must not contain fake python environment");
    assert(!emptyRunHtml.includes("ruff check --no-cache ."), "Must not contain fake ruff command");

    console.log("  ✓ EvidenceLedger 4 proof cards (pytest, ruff, changed files, invariants) verified");
  }

  // ── Test 20: RightDrawer Tab Navigation & Width Toggling ──────────────────
  console.log("\n[Test 20] RightDrawer Shell, Tab Navigation & Width Toggling");
  {
    // Terminal Tab view
    const terminalHtml = renderToString(
      React.createElement(RightDrawer, {
        run: sampleRun,
        evidence: sampleEvidence,
        activeTab: "terminal",
      })
    );
    assert(terminalHtml.includes("data-testid=\"right-drawer-root\""), "Must render drawer root");
    assert(terminalHtml.includes("data-testid=\"drawer-tab-terminal\""), "Must render terminal tab");
    assert(terminalHtml.includes("data-testid=\"drawer-tab-diff\""), "Must render diff tab");
    assert(terminalHtml.includes("data-testid=\"drawer-tab-evidence\""), "Must render evidence tab");
    assert(terminalHtml.includes("Terminal &amp; Logi") || terminalHtml.includes("Terminal & Logi"), "Must have Polish label");
    assert(terminalHtml.includes("Unified Diff (2)"), "Diff tab must show changed files count (2)");
    assert(terminalHtml.includes("Księga Dowodów"), "Must have Polish Evidence Ledger label");
    assert(terminalHtml.includes("data-testid=\"terminal-viewport\""), "Terminal tab must render viewport");

    // Diff Tab view
    const diffHtml = renderToString(
      React.createElement(RightDrawer, {
        run: sampleRun,
        evidence: sampleEvidence,
        activeTab: "diff",
      })
    );
    assert(diffHtml.includes("data-testid=\"diff-viewport\""), "Diff tab must render diff viewport");

    // Evidence Tab view
    const evidenceHtml = renderToString(
      React.createElement(RightDrawer, {
        run: sampleRun,
        evidence: sampleEvidence,
        activeTab: "evidence",
      })
    );
    assert(evidenceHtml.includes("data-testid=\"evidence-ledger-container\""), "Evidence tab must render ledger container");

    // Width attribute verification
    const customWidthHtml = renderToString(
      React.createElement(RightDrawer, {
        run: sampleRun,
        evidence: sampleEvidence,
        activeTab: "terminal",
        width: 440,
      })
    );
    assert(customWidthHtml.includes("width:440px") || customWidthHtml.includes("width: 440px"), "Must support 440px custom width");

    assert(terminalHtml.includes("data-testid=\"close-right-drawer\""), "Must render close button");
    assert(terminalHtml.includes("data-testid=\"toggle-drawer-width\""), "Must render width toggle button");
    assert(terminalHtml.includes("data-testid=\"right-drawer-footer\""), "Must render status footer");

    console.log("  ✓ RightDrawer tab switcher, active component mounting, and responsive width verified");
  }

  console.log("\n=== ALL RIGHT DRAWER & TELEMETRY CONSOLE TESTS PASSED ===");
}
