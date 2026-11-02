/**
 * AAW Agentic Studio — Frontend Unit & Contract Tests
 * Validates typed API client, token handling, error conditions,
 * terminal state logic, and reactive store behavior.
 */

import assert from "node:assert";
import { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import { createEngineStore, isTerminalOrHoldState } from "../store/engineStore";

async function runTests() {
  console.log("=== Starting AAW Frontend Unit Tests ===");

  // ── Test 1: ApiClient Token Resolution & Missing Token Error ──────────────
  console.log("\n[Test 1] ApiClient Token Ingestion & Auth Guard");
  {
    const client = new ApiClient({ token: "test-token-123" });
    assert.strictEqual(client.getToken(), "test-token-123", "Explicit token should be returned");

    const noTokenClient = new ApiClient();
    try {
      await noTokenClient.getBootstrap();
      assert.fail("Should have thrown ApiError when token is missing");
    } catch (err: any) {
      assert(err instanceof ApiError, "Error must be instance of ApiError");
      assert.strictEqual(err.status, 403, "Status should be 403");
      assert(err.message.includes("authentication token is missing"), "Error message should mention missing token");
    }
    console.log("  ✓ Token resolution and 403 guard passed");
  }

  // ── Test 2: ApiClient Endpoint Routing & Headers ──────────────────────────
  console.log("\n[Test 2] ApiClient Fetch Execution & Header Injection");
  {
    const mockCalls: { url: string; method: string; headers: any; body?: any }[] = [];
    const originalFetch = globalThis.fetch;

    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const method = init?.method || "GET";
      const headers = init?.headers || {};
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      mockCalls.push({ url, method, headers, body });

      if (url.endsWith("/api/bootstrap")) {
        return new Response(JSON.stringify({ version: "AAW 0.4.0", presets: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/runs")) {
        return new Response(JSON.stringify({ sections: { running: [], paused: [], attention: [], completed: [] } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/runs/test-run-1/evidence")) {
        return new Response(JSON.stringify({ run_dir: "/path/to/run", latest_diff: "diff --git a/file" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/runs/test-run-1")) {
        return new Response(
          JSON.stringify({
            run_id: "test-run-1",
            status: "RUNNING",
            engine: { status: "RUNNING", phase: "EXECUTE" },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/runs/test-run-1/decision")) {
        return new Response(JSON.stringify({ status: "ACCEPTED", action: body?.action }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/runs/test-run-1/stop")) {
        return new Response(JSON.stringify({ status: "STOPPING" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/runs/test-run-1/resume")) {
        return new Response(JSON.stringify({ status: "RESUMED" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/mcp/status")) {
        return new Response(
          JSON.stringify({
            antigravity: { configured: true },
            claude: { configured: false },
            codex: { configured: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      if (url.endsWith("/api/mcp/configure")) {
        return new Response(JSON.stringify({ target: "antigravity", installed: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/start")) {
        return new Response(JSON.stringify({ run_id: "new-run-99", status: "STARTED" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      return new Response(JSON.stringify({ error: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    };

    try {
      const client = new ApiClient({ token: "token-abc" });

      const bootstrap = await client.getBootstrap();
      assert.strictEqual(bootstrap.version, "AAW 0.4.0");

      const runs = await client.getRuns();
      assert(Array.isArray(runs.sections.running));

      const run = await client.getRun("test-run-1");
      assert.strictEqual(run.run_id, "test-run-1");
      assert.strictEqual(run.status, "RUNNING");

      const evidence = await client.getEvidence("test-run-1");
      assert(evidence.latest_diff?.includes("diff --git"));

      const decision = await client.postDecision("test-run-1", "ACCEPT", "Approved by human");
      assert.strictEqual(decision.action, "ACCEPT");

      const stop = await client.postStop("test-run-1", false);
      assert.strictEqual(stop.status, "STOPPING");

      const resume = await client.postResume("test-run-1", "lock-xyz");
      assert.strictEqual(resume.status, "RESUMED");

      const mcp = await client.getMCPStatus();
      assert.strictEqual(mcp.antigravity.configured, true);

      const mcpConfig = await client.configureMCP("antigravity");
      assert.strictEqual(mcpConfig.installed, true);

      const start = await client.postStart({ repo: "c:/repo", goal: "Add feature" });
      assert.strictEqual(start.run_id, "new-run-99");

      // Verify all requests had X-AAW-Token header
      assert(mockCalls.length >= 9, "Should have executed at least 9 API calls");
      for (const call of mockCalls) {
        assert.strictEqual(call.headers["X-AAW-Token"], "token-abc", `Call to ${call.url} must contain X-AAW-Token`);
      }

      console.log(`  ✓ All ${mockCalls.length} API methods successfully executed with X-AAW-Token header`);
    } finally {
      globalThis.fetch = originalFetch;
    }
  }

  // ── Test 3: isTerminalOrHoldState Helper ───────────────────────────────────
  console.log("\n[Test 3] Terminal and Hold State Classifier");
  {
    assert.strictEqual(isTerminalOrHoldState(null), true, "null run is treated as hold/terminal");

    // Running states -> false
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "RUNNING", engine: { status: "RUNNING" } } as any),
      false,
      "RUNNING should not be terminal"
    );

    // Human Gate hold states -> true
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "READY_FOR_DECISION", engine: { status: "AWAITING_HUMAN" } } as any),
      true,
      "AWAITING_HUMAN must pause polling"
    );
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "NEEDS_ATTENTION", engine: { status: "AWAITING_HUMAN" } } as any),
      true,
      "NEEDS_ATTENTION must pause polling"
    );

    // Completed states -> true
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "ACCEPTED", engine: { status: "HUMAN_APPROVED" } } as any),
      true,
      "HUMAN_APPROVED must pause polling"
    );
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "ACCEPTED", engine: { status: "PROMOTED" } } as any),
      true,
      "PROMOTED must pause polling"
    );

    // Paused / Interrupted states -> true
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "PAUSED", engine: { status: "RUNNING" } } as any),
      true,
      "PAUSED must pause polling"
    );
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "INTERRUPTED", engine: { status: "RUNNING" } } as any),
      true,
      "INTERRUPTED must pause polling"
    );

    // Failed states -> true
    assert.strictEqual(
      isTerminalOrHoldState({ run_id: "1", status: "START_FAILED", engine: { status: "REJECTED" } } as any),
      true,
      "START_FAILED must pause polling"
    );

    console.log("  ✓ All lifecycle states correctly classified for polling pause/resume");
  }

  // ── Test 4: EngineStore Reactivity and Polling ─────────────────────────────
  console.log("\n[Test 4] EngineStore State Management & Polling Transitions");
  {
    let fetchCount = 0;
    const testClient = new ApiClient({ token: "store-token" });

    // Mock API client methods
    testClient.getRun = async (id: string): Promise<any> => {
      fetchCount += 1;
      if (fetchCount <= 2) {
        return {
          run_id: id,
          status: "RUNNING",
          engine: { status: "RUNNING", phase: "EXECUTE" },
        };
      }
      // On 3rd fetch, transition to AWAITING_HUMAN
      return {
        run_id: id,
        status: "READY_FOR_DECISION",
        engine: { status: "AWAITING_HUMAN", phase: "REVIEW" },
      };
    };

    testClient.getEvidence = async (id: string): Promise<any> => {
      return { run_dir: `/runs/${id}`, latest_diff: "sample-diff" };
    };

    testClient.postDecision = async (_id: string, action: any, feedback?: string): Promise<any> => {
      return { status: "DECIDED", action, feedback };
    };

    const store = createEngineStore(testClient);
    assert.strictEqual(store.getState().activeRunId, null);
    assert.strictEqual(store.getState().selectedTab, "terminal");
    assert.strictEqual(store.getState().roleFilter, "All");

    // Tab and filter mutation
    store.actions.setSelectedTab("diff");
    assert.strictEqual(store.getState().selectedTab, "diff");

    store.actions.setRoleFilter("Verifier");
    assert.strictEqual(store.getState().roleFilter, "Verifier");

    // Select run -> starts polling because it's RUNNING
    await store.actions.selectRun("run-42");
    assert.strictEqual(store.getState().activeRunId, "run-42");
    assert.strictEqual(store.getState().run?.status, "RUNNING");
    assert.strictEqual(store.getState().pollingActive, true, "Polling must be active for RUNNING run");

    // Wait for polling ticks (1000ms each)
    console.log("  Waiting for polling loop to transition to AWAITING_HUMAN...");
    await new Promise((resolve) => setTimeout(resolve, 2500));

    // After 2-3 ticks, the run should have transitioned to AWAITING_HUMAN and stopped polling
    assert.strictEqual(
      store.getState().run?.engine?.status,
      "AWAITING_HUMAN",
      "Run state must be updated to AWAITING_HUMAN by poller"
    );
    assert.strictEqual(
      store.getState().pollingActive,
      false,
      "Polling must pause automatically on AWAITING_HUMAN hold state"
    );

    // Submit decision
    await store.actions.submitDecision("CONTINUE", "Keep going");
    assert.strictEqual(store.getState().error, null);

    store.stopPolling();
    console.log("  ✓ EngineStore reactive polling and hold pause verified successfully");
  }

  console.log("\n=== ALL FRONTEND UNIT TESTS PASSED ===");
}

runTests().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
