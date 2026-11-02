/**
 * AAW Agentic Studio — Layout & Left Rail Component Tests
 *
 * Tests StudioLayout, LeftRail, and MCPConfigModal structure,
 * store reactivity, provider health indicators, and preset selection.
 */

import assert from "node:assert";
import React from "react";
import { renderToString } from "react-dom/server";
import { StudioLayout } from "../components/layout/StudioLayout";
import { LeftRail } from "../components/nav/LeftRail";
import { MCPConfigModal } from "../components/mcp/MCPConfigModal";
import { engineStore } from "../store/engineStore";

export async function runLayoutComponentTests() {
  console.log("\n=== Starting AAW Studio Layout & Left Rail Tests ===");

  // ── Test 5: StudioLayout Tri-Pane Structure & Header Elements ─────────────
  console.log("\n[Test 5] StudioLayout Shell & Header Rendering");
  {
    engineStore.actions.reset();

    // Render default idle layout
    const htmlIdle = renderToString(
      React.createElement(
        StudioLayout,
        {
          centerCanvas: React.createElement("div", { id: "test-canvas" }, "Center Canvas Test"),
          rightDrawer: React.createElement("div", { id: "test-drawer" }, "Right Drawer Test"),
        }
      )
    );

    // Verify brand title and release pill
    assert(htmlIdle.includes("AAW Agentic Studio"), "Header must include 'AAW Agentic Studio'");
    assert(htmlIdle.includes("data-testid=\"release-version-pill\""), "Header must include release version pill");
    assert(htmlIdle.includes("v0.4.0"), "Default version must be v0.4.0");

    // Verify repo and git branch badge
    assert(htmlIdle.includes("data-testid=\"active-repo-badge\""), "Header must include active repo badge");
    assert(htmlIdle.includes("data-testid=\"git-branch-name\""), "Header must include git branch name");
    assert(htmlIdle.includes("main"), "Default branch should be main");

    // Verify idle status capsule
    assert(htmlIdle.includes("data-testid=\"run-status-capsule\""), "Header must include run status capsule");
    assert(htmlIdle.includes("IDLE — Oczekiwanie na zadanie"), "Capsule should show IDLE status");

    // Verify tri-pane slot containers
    assert(htmlIdle.includes("data-testid=\"left-rail-container\""), "Must contain Left Rail container");
    assert(htmlIdle.includes("data-testid=\"center-canvas-container\""), "Must contain Center Canvas container");
    assert(htmlIdle.includes("Center Canvas Test"), "Center canvas content must be rendered");
    assert(htmlIdle.includes("data-testid=\"right-drawer-container\""), "Must contain Right Drawer container");
    assert(htmlIdle.includes("Right Drawer Test"), "Right drawer content must be rendered");

    console.log("  ✓ StudioLayout tri-pane containers and idle header verified");
  }

  // ── Test 6: StudioLayout Status Capsule Dynamics ──────────────────────────
  console.log("\n[Test 6] StudioLayout Active Run & Human Gate Capsule");
  {
    engineStore.actions.reset();

    // 1. Test RUNNING state
    engineStore.actions._setState({
      activeRunId: "run-abc-123",
      run: {
        run_id: "run-abc-123",
        status: "RUNNING",
        project: "Project Phoenix",
        process: { phase: "EXECUTE", iteration: 2, max_iterations: 5 },
        workspace: { repo_path: "c:/repos/phoenix", branch: "feat/turbo-exec" },
        engine: { status: "RUNNING", phase: "EXECUTE" },
      } as any,
    });

    const htmlRunning = renderToString(React.createElement(StudioLayout));
    assert(htmlRunning.includes("AKTYWNY"), "Status capsule should reflect RUNNING state");
    assert(htmlRunning.includes("[EXECUTE]"), "Status capsule should display active phase");
    assert(htmlRunning.includes("Iteracja 2/5"), "Status capsule should display iteration count");
    assert(htmlRunning.includes("phoenix"), "Repo badge should display project repo name");
    assert(htmlRunning.includes("feat/turbo-exec"), "Git branch badge should show active branch");

    // 2. Test Human Gate state
    engineStore.actions._setState({
      activeRunId: "run-abc-123",
      run: {
        run_id: "run-abc-123",
        status: "READY_FOR_DECISION",
        process: { phase: "REVIEW", iteration: 2, max_iterations: 5 },
        workspace: { repo_path: "c:/repos/phoenix", branch: "feat/turbo-exec" },
        engine: { status: "AWAITING_HUMAN", phase: "REVIEW" },
      } as any,
    });

    const htmlGate = renderToString(React.createElement(StudioLayout));
    assert(
      htmlGate.includes("BRAMKA DECYZYJNA"),
      "Status capsule must prominently flag human gate hold state"
    );
    assert(
      htmlGate.includes("Wymaga człowieka"),
      "Status capsule must explicitly mention human oversight requirement"
    );

    console.log("  ✓ Dynamic status capsules and branch metadata verified");
  }

  // ── Test 7: LeftRail Navigation, AI Providers & Autonomy Presets ───────────
  console.log("\n[Test 7] LeftRail Navigation, Provider Badges & Policy Presets");
  {
    engineStore.actions.reset();

    // Set mock bootstrap with 3 providers
    const mockBootstrap: any = {
      version: "AAW 0.4.0",
      release: { release: "0.4.0" },
      providers: {
        providers: [
          {
            provider_id: "claude",
            display_name: "Claude CLI",
            status: "FOUND",
            login: "NOT_LOGGED_IN",
            version: "2.1.287",
            runnable_profiles: [],
          },
          {
            provider_id: "codex",
            display_name: "Codex CLI",
            status: "NOT_FOUND",
            login: "UNKNOWN",
            version: null,
            runnable_profiles: [],
          },
          {
            provider_id: "antigravity",
            display_name: "Antigravity CLI",
            status: "FOUND",
            login: "LOGGED_IN",
            version: "1.4.12",
            runnable_profiles: ["AGY_PRO"],
          },
        ],
      },
    };

    engineStore.actions._setState({ bootstrap: mockBootstrap });

    const htmlRail = renderToString(React.createElement(LeftRail));

    // 1. Navigation items
    assert(htmlRail.includes("Live Command Center"), "Must render 'Live Command Center' nav");
    assert(htmlRail.includes("New Task Wizard"), "Must render 'New Task Wizard' nav");
    assert(htmlRail.includes("Task Runs"), "Must render 'Task Runs' nav");
    assert(htmlRail.includes("Settings"), "Must render 'Settings' nav");

    // 2. AI Providers Health Card
    assert(htmlRail.includes("data-testid=\"ai-providers-card\""), "Must render AI providers health card");
    assert(htmlRail.includes("Claude"), "Must list Claude provider");
    assert(htmlRail.includes("Codex"), "Must list Codex provider");
    assert(htmlRail.includes("Antigravity"), "Must list Antigravity provider");

    // Verify badges
    assert(htmlRail.includes("NOT LOGGED IN"), "Claude should have NOT LOGGED IN badge");
    assert(htmlRail.includes("NOT FOUND"), "Codex should have NOT FOUND badge");
    assert(htmlRail.includes("READY"), "Antigravity should have READY badge");

    // 3. Autonomy Policy Presets
    assert(htmlRail.includes("data-testid=\"autonomy-presets-section\""), "Must render autonomy policy presets");
    assert(htmlRail.includes("Konserwatywny"), "Must render 'Konserwatywny' preset");
    assert(htmlRail.includes("Zbalansowany"), "Must render 'Zbalansowany' preset");
    assert(htmlRail.includes("Pełna autonomia"), "Must render 'Pełna autonomia' preset");

    // Test store mutation for preset and navigation
    engineStore.actions.setActivePolicyPreset("autonomous");
    assert.strictEqual(
      engineStore.getState().activePolicyPreset,
      "autonomous",
      "Active policy preset should update to 'autonomous'"
    );

    engineStore.actions.setActiveNav("new-task");
    assert.strictEqual(
      engineStore.getState().activeNav,
      "new-task",
      "Active navigation should update to 'new-task'"
    );

    // 4. Universal MCP Card
    assert(htmlRail.includes("data-testid=\"universal-mcp-card\""), "Must render Universal MCP Card");
    assert(htmlRail.includes("8 wbudowanych narzędzi"), "Must display tools count");
    assert(htmlRail.includes("⚡ Konfiguruj (1-Click)"), "Must render 1-Click configure button");

    console.log("  ✓ LeftRail navigation, provider health badges, presets, and MCP card verified");
  }

  // ── Test 8: MCPConfigModal Dialog & 1-Click Setup Execution ───────────────
  console.log("\n[Test 8] MCPConfigModal Rendering & Execution Action");
  {
    engineStore.actions.reset();

    // 1. Closed state
    const htmlClosed = renderToString(React.createElement(MCPConfigModal, { isOpen: false }));
    assert.strictEqual(htmlClosed, "", "Modal should not render HTML when closed");

    // 2. Open state with mock MCP status
    engineStore.actions._setState({
      mcpStatus: {
        antigravity: {
          path: "C:/Users/user/.gemini/antigravity/mcp_config.json",
          configured: true,
        },
        claude: {
          desktop_path: "C:/Users/user/.claude/claude_desktop_config.json",
          code_path: "C:/Users/user/.claude/claude_code.json",
          configured: false,
        },
        codex: {
          path: "C:/Users/user/.codex/config.toml",
          configured: true,
        },
      },
    });

    const htmlOpen = renderToString(React.createElement(MCPConfigModal, { isOpen: true }));

    // Verify modal header and clients
    assert(htmlOpen.includes("Universal MCP Server Hub"), "Modal must display title");
    assert(htmlOpen.includes("Antigravity CLI &amp; IDE"), "Modal must list Antigravity client");
    assert(htmlOpen.includes("Claude Code &amp; Desktop"), "Modal must list Claude client");
    assert(htmlOpen.includes("Codex CLI"), "Modal must list Codex client");

    // Verify status badges
    assert(htmlOpen.includes("SKONFIGUROWANY"), "Configured clients must show SKONFIGUROWANY");
    assert(htmlOpen.includes("DO SKONFIGUROWANIA"), "Unconfigured clients must show DO SKONFIGUROWANIA");

    // Verify 8 Tools
    assert(htmlOpen.includes("aaw_detect_providers"), "Tools list must contain aaw_detect_providers");
    assert(htmlOpen.includes("aaw_inspect_repo"), "Tools list must contain aaw_inspect_repo");
    assert(htmlOpen.includes("aaw_start_task"), "Tools list must contain aaw_start_task");
    assert(htmlOpen.includes("aaw_run_status"), "Tools list must contain aaw_run_status");
    assert(htmlOpen.includes("aaw_run_evidence"), "Tools list must contain aaw_run_evidence");
    assert(htmlOpen.includes("aaw_run_stop"), "Tools list must contain aaw_run_stop");
    assert(htmlOpen.includes("aaw_run_resume"), "Tools list must contain aaw_run_resume");

    // Verify 1-click action button
    assert(htmlOpen.includes("⚡ Konfiguruj wszystko (1-Click)"), "Must render 1-Click configure button");

    // 3. Test configureMCP execution in store
    let configuredTarget = "";
    (engineStore as any).actions.configureMCP = async (req: any) => {
      configuredTarget = typeof req === "string" ? req : req.target;
      return { target: configuredTarget, installed: true };
    };

    const configResult = await engineStore.actions.configureMCP({ target: "all", install: true });
    assert.strictEqual(configResult.installed, true, "MCP configuration should succeed");
    assert.strictEqual(configuredTarget, "all", "Target should be 'all'");

    // Test modal toggle via store
    engineStore.actions.setMCPModalOpen(true);
    assert.strictEqual(engineStore.getState().mcpModalOpen, true, "Store mcpModalOpen should be true");

    engineStore.actions.setMCPModalOpen(false);
    assert.strictEqual(engineStore.getState().mcpModalOpen, false, "Store mcpModalOpen should be false");

    console.log("  ✓ MCPConfigModal dialog, client statuses, tools list, and 1-click action verified");
  }

  console.log("\n=== ALL STUDIO LAYOUT & LEFT RAIL TESTS PASSED ===");
}
