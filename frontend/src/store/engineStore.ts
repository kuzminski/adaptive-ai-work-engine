/**
 * AAW Agentic Studio — Reactive Engine Store
 *
 * Provides application-wide state management and background polling
 * for the AAW 7-phase engine, human gate, evidence ledger, and MCP hub.
 */

import { useSyncExternalStore } from "react";
import { client as defaultClient, ApiClient } from "../api/client";
import {
  BootstrapData,
  ContinuationDraft,
  DecisionAction,
  DrawerTab,
  EvidenceView,
  MCPConfigureRequest,
  MCPConfigureResponse,
  MCPStatus,
  RoleFilter,
  RunDetail,
  RunsResponse,
  StartTaskPayload,
  TaskStartResult,
} from "../api/types";

export type NavItem = "command-center" | "new-task" | "runs" | "experience" | "settings";
export type AutonomyPreset = "conservative" | "balanced" | "autonomous";

export interface EngineStoreState {
  bootstrap: BootstrapData | null;
  activeRunId: string | null;
  run: RunDetail | null;
  evidence: EvidenceView | null;
  mcpStatus: MCPStatus | null;
  runs: RunsResponse | null;
  loading: boolean;
  error: string | null;
  selectedTab: DrawerTab;
  roleFilter: RoleFilter;
  pollingActive: boolean;
  activeNav: NavItem;
  activePolicyPreset: AutonomyPreset;
  continuationDraft: ContinuationDraft | null;
  mcpModalOpen: boolean;
  rightDrawerOpen: boolean;
}

const initialState: EngineStoreState = {
  bootstrap: null,
  activeRunId: null,
  run: null,
  evidence: null,
  mcpStatus: null,
  runs: null,
  loading: false,
  error: null,
  selectedTab: "terminal",
  roleFilter: "All",
  pollingActive: false,
  activeNav: "command-center",
  activePolicyPreset: "balanced",
  continuationDraft: null,
  mcpModalOpen: false,
  rightDrawerOpen: true,
};

/**
 * Helper to determine if a run has reached a terminal or hold state
 * where autonomous execution is paused awaiting human input or completed.
 */
export function isTerminalOrHoldState(run: RunDetail | null): boolean {
  if (!run) return true;

  const engineStatus = run.engine?.status;
  const status = run.status;

  // Hold / Human Gate state:
  if (
    engineStatus === "AWAITING_HUMAN" ||
    status === "READY_FOR_DECISION" ||
    status === "NEEDS_ATTENTION"
  ) {
    return true;
  }

  // Terminal completion states:
  if (
    engineStatus === "HUMAN_APPROVED" ||
    engineStatus === "PROMOTED" ||
    status === "ACCEPTED"
  ) {
    return true;
  }

  // Stopped or paused states:
  if (status === "PAUSED" || status === "INTERRUPTED") {
    return true;
  }

  // Failure or rejection states:
  if (
    engineStatus === "REJECTED" ||
    status === "REJECTED" ||
    status === "START_FAILED"
  ) {
    return true;
  }

  return false;
}

export function createEngineStore(api: ApiClient = defaultClient) {
  let state: EngineStoreState = { ...initialState };
  const listeners = new Set<() => void>();
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let pollInFlight = false;
  let activeRunSeq = 0;

  function getState(): EngineStoreState {
    return state;
  }

  function setState(partial: Partial<EngineStoreState> | ((prev: EngineStoreState) => Partial<EngineStoreState>)): void {
    const next = typeof partial === "function" ? partial(state) : partial;
    state = { ...state, ...next };
    listeners.forEach((listener) => listener());
  }

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }

  function stopPolling(): void {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    setState({ pollingActive: false });
  }

  function startPolling(): void {
    if (pollTimer) {
      clearInterval(pollTimer);
    }
    setState({ pollingActive: true });
    pollTimer = setInterval(async () => {
      if (pollInFlight || !state.activeRunId || !state.pollingActive) {
        return;
      }
      pollInFlight = true;
      const targetRunId = state.activeRunId;
      const currentSeq = activeRunSeq;

      try {
        const updatedRun = await api.getRun(targetRunId);
        // Only commit if the run didn't change while fetch was in-flight
        if (state.activeRunId === targetRunId && activeRunSeq === currentSeq) {
          const terminal = isTerminalOrHoldState(updatedRun);

          setState({
            run: updatedRun,
            error: null,
          });

          if (terminal) {
            stopPolling();
            // Automatically refresh evidence when transitioning to human gate
            if (updatedRun.engine?.status === "AWAITING_HUMAN" || updatedRun.status === "READY_FOR_DECISION") {
              try {
                const freshEvidence = await api.getEvidence(targetRunId);
                if (state.activeRunId === targetRunId) {
                  setState({ evidence: freshEvidence });
                }
              } catch {
                // Evidence fetch failure is non-fatal for gate display
              }
            }
          }
        }
      } catch (err: any) {
        if (state.activeRunId === targetRunId && activeRunSeq === currentSeq) {
          setState({ error: err?.message || String(err) });
        }
      } finally {
        pollInFlight = false;
      }
    }, 1000);
  }

  // ── Actions ──────────────────────────────────────────────────────────────

  const actions = {
    loadBootstrap: async (): Promise<void> => {
      setState({ loading: true });
      try {
        const [bootstrap, mcpStatus, runs] = await Promise.all([
          api.getBootstrap(),
          api.getMCPStatus().catch(() => null),
          api.getRuns().catch(() => null),
        ]);
        setState({
          bootstrap,
          mcpStatus,
          runs,
          loading: false,
          error: null,
        });
      } catch (err: any) {
        setState({
          loading: false,
          error: err?.message || "Failed to load bootstrap data",
        });
      }
    },

    loadMCPStatus: async (): Promise<void> => {
      try {
        const mcpStatus = await api.getMCPStatus();
        setState({ mcpStatus, error: null });
      } catch (err: any) {
        setState({ error: err?.message || "Failed to load MCP status" });
      }
    },

    configureMCP: async (
      targets?: string | MCPConfigureRequest
    ): Promise<MCPConfigureResponse> => {
      try {
        const res = await api.configureMCP(targets);
        // Refresh MCP status after configuration
        const mcpStatus = await api.getMCPStatus().catch(() => null);
        if (mcpStatus) {
          setState({ mcpStatus });
        }
        return res;
      } catch (err: any) {
        const msg = err?.message || "Failed to configure MCP";
        setState({ error: msg });
        throw err;
      }
    },

    loadRuns: async (): Promise<void> => {
      try {
        const runs = await api.getRuns();
        setState({ runs, error: null });
      } catch (err: any) {
        setState({ error: err?.message || "Failed to load runs list" });
      }
    },

    selectRun: async (runId: string): Promise<void> => {
      stopPolling();
      activeRunSeq += 1;
      const currentSeq = activeRunSeq;

      setState({
        activeRunId: runId,
        loading: true,
        error: null,
      });

      try {
        const [run, evidence] = await Promise.all([
          api.getRun(runId),
          api.getEvidence(runId).catch(() => null),
        ]);

        if (activeRunSeq === currentSeq) {
          setState({
            run,
            evidence,
            loading: false,
            error: null,
          });

          // If the run is active and not in terminal/hold state, start polling
          if (!isTerminalOrHoldState(run)) {
            startPolling();
          }
        }
      } catch (err: any) {
        if (activeRunSeq === currentSeq) {
          setState({
            loading: false,
            error: err?.message || `Failed to load run ${runId}`,
          });
        }
      }
    },

    refreshActiveRun: async (): Promise<void> => {
      if (!state.activeRunId) return;
      const targetId = state.activeRunId;
      try {
        const run = await api.getRun(targetId);
        if (state.activeRunId === targetId) {
          setState({ run, error: null });
          if (!isTerminalOrHoldState(run) && !state.pollingActive) {
            startPolling();
          } else if (isTerminalOrHoldState(run) && state.pollingActive) {
            stopPolling();
          }
        }
      } catch (err: any) {
        setState({ error: err?.message || "Failed to refresh run" });
      }
    },

    refreshEvidence: async (executionId?: string): Promise<void> => {
      if (!state.activeRunId) return;
      try {
        const evidence = await api.getEvidence(state.activeRunId, executionId);
        setState({ evidence, error: null });
      } catch (err: any) {
        setState({ error: err?.message || "Failed to refresh evidence" });
      }
    },

    submitDecision: async (
      action: DecisionAction,
      feedback?: string
    ): Promise<any> => {
      if (!state.activeRunId) {
        throw new Error("No active run selected for decision");
      }
      const targetId = state.activeRunId;
      setState({ loading: true, error: null });

      try {
        const result = await api.postDecision(targetId, action, feedback);
        // Refresh run state and evidence immediately after decision
        const [updatedRun, updatedEvidence] = await Promise.all([
          api.getRun(targetId),
          api.getEvidence(targetId).catch(() => null),
        ]);

        const bannerNotice =
          action === "CONTINUE" && (result?.prefill || result?.ok)
            ? "Przygotowano kontynuację zadania (draft). Możesz przejść do nowego zadania."
            : null;

        setState({
          run: updatedRun,
          evidence: updatedEvidence || state.evidence,
          loading: false,
          error: bannerNotice,
        });

        // If continuing or approved and active, restart polling
        if (!isTerminalOrHoldState(updatedRun)) {
          startPolling();
        } else {
          stopPolling();
        }

        return result;
      } catch (err: any) {
        setState({
          loading: false,
          error: err?.message || "Failed to submit decision",
        });
        throw err;
      }
    },

    stopRun: async (force: boolean = false): Promise<any> => {
      if (!state.activeRunId) return;
      setState({ loading: true });
      try {
        const res = await api.postStop(state.activeRunId, force);
        const updated = await api.getRun(state.activeRunId);
        setState({
          run: updated,
          loading: false,
        });
        if (isTerminalOrHoldState(updated)) {
          stopPolling();
        }
        return res;
      } catch (err: any) {
        setState({
          loading: false,
          error: err?.message || "Failed to stop run",
        });
        throw err;
      }
    },

    resumeRun: async (): Promise<any> => {
      if (!state.activeRunId) return;
      setState({ loading: true });
      try {
        const lockToken = state.run?.controls?.lock_token || null;
        const res = await api.postResume(state.activeRunId, lockToken);
        const updated = await api.getRun(state.activeRunId);
        setState({
          run: updated,
          loading: false,
        });
        if (!isTerminalOrHoldState(updated)) {
          startPolling();
        }
        return res;
      } catch (err: any) {
        setState({
          loading: false,
          error: err?.message || "Failed to resume run",
        });
        throw err;
      }
    },

    startRun: async (
      payload: StartTaskPayload | { form: any }
    ): Promise<TaskStartResult> => {
      setState({ loading: true, error: null });
      try {
        const res = await api.postStart(payload);
        setState({ loading: false });
        if (res?.run_id) {
          await actions.selectRun(res.run_id);
        }
        return res;
      } catch (err: any) {
        setState({
          loading: false,
          error: err?.message || "Failed to start task",
        });
        throw err;
      }
    },

    setSelectedTab: (tab: DrawerTab): void => {
      setState({ selectedTab: tab });
    },

    setRoleFilter: (filter: RoleFilter): void => {
      setState({ roleFilter: filter });
    },

    setActiveNav: (nav: NavItem): void => {
      setState({ activeNav: nav });
    },

    /**
     * Roadmap 2.2: ask the backend for an editable continuation (same goal, unfinished points) and open the wizard with it.
     * Accepts the candidate at the Human Gate first when it is promotable (same as "Kontynuuj"); starts nothing.
     */
    prepareRoadmapEdit: async (): Promise<void> => {
      if (!state.activeRunId) return;
      setState({ loading: true, error: null });
      try {
        const result = await api.postDecision(state.activeRunId, "CONTINUE", undefined, { mode: "roadmap" });
        if (!result?.prefill) throw new Error("Brak wersji roboczej kontynuacji");
        setState({ continuationDraft: result.prefill, activeNav: "new-task", loading: false });
      } catch (err: any) {
        setState({ loading: false, error: err?.message || "Nie udało się przygotować edycji roadmapy" });
      }
    },

    clearContinuationDraft: (): void => {
      setState({ continuationDraft: null });
    },

    setActivePolicyPreset: (preset: AutonomyPreset): void => {
      setState({ activePolicyPreset: preset });
    },

    setMCPModalOpen: (open: boolean): void => {
      setState({ mcpModalOpen: open });
    },

    setRightDrawerOpen: (open: boolean): void => {
      setState({ rightDrawerOpen: open });
    },

    toggleRightDrawer: (): void => {
      setState((prev) => ({ rightDrawerOpen: !prev.rightDrawerOpen }));
    },

    clearError: (): void => {
      setState({ error: null });
    },

    setPollingActive: (active: boolean): void => {
      if (active) {
        startPolling();
      } else {
        stopPolling();
      }
    },

    startPolling: (): void => {
      startPolling();
    },

    stopPolling: (): void => {
      stopPolling();
    },

    reset: (): void => {
      stopPolling();
      setState({ ...initialState });
    },

    _setState: (partial: Partial<EngineStoreState>): void => {
      setState(partial);
    },
  };

  return {
    getState,
    subscribe,
    actions,
    startPolling,
    stopPolling,
  };
}

// Global default store instance
export const engineStore = createEngineStore();

/**
 * Primary React Hook for consuming the Engine Store.
 * Provides reactive state and bound actions for components.
 */
export function useEngineStore() {
  const storeState = useSyncExternalStore(
    engineStore.subscribe,
    engineStore.getState,
    engineStore.getState
  );

  return {
    ...storeState,
    ...engineStore.actions,
  };
}
