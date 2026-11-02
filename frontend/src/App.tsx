/**
 * AAW Agentic Studio — Main Application Component
 *
 * Mounts the StudioLayout with live store binding, LeftRail navigation,
 * CenterCanvas execution view, and RightDrawer telemetry console.
 */

import { useEffect, useState } from "react";
import {
  ShieldAlert,
  RefreshCw,
  Radio,
  AlertTriangle,
} from "lucide-react";
import { client } from "./api/client";
import { DecisionAction } from "./api/types";
import { useEngineStore } from "./store/engineStore";
import { StudioLayout } from "./components/layout/StudioLayout";
import { LeftRail } from "./components/nav/LeftRail";
import { PhaseGraph } from "./components/graph/PhaseGraph";
import { HumanGateBanner } from "./components/gate/HumanGateBanner";
import { GateDecisionModal } from "./components/gate/GateDecisionModal";
import { RightDrawer } from "./components/drawer/RightDrawer";

export function App() {
  const {
    activeRunId,
    run,
    loading,
    error,
    pollingActive,
    activePolicyPreset,
    loadBootstrap,
    clearError,
    startPolling,
    stopPolling,
  } = useEngineStore();

  const [gateModalMode, setGateModalMode] = useState<DecisionAction | null>(null);

  const token = client.getToken() || "none";

  useEffect(() => {
    // Attempt automatic bootstrap on mount if served by AAW server
    if (token && token !== "none" && token !== "__AAW_TOKEN__") {
      loadBootstrap();
    }
  }, [token, loadBootstrap]);

  // Center Canvas Content
  const centerCanvas = (
    <div className="max-w-5xl w-full mx-auto space-y-6 animate-fade-in" data-testid="center-canvas-content">
      {/* Error notification banner */}
      {error && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-xl flex items-center justify-between text-rose-300 text-xs">
          <div className="flex items-center gap-2.5">
            <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
            <span>{error}</span>
          </div>
          <button
            onClick={clearError}
            className="text-xs px-2.5 py-1 bg-rose-500/20 hover:bg-rose-500/30 rounded text-rose-200 transition font-mono"
          >
            ✕ Odrzuć
          </button>
        </div>
      )}

      {/* Sticky Human Gate Decision Banner */}
      <HumanGateBanner
        run={run}
        onSelectAction={(action) => setGateModalMode(action)}
        isModalOpen={gateModalMode !== null}
      />

      {/* Hero / Run Header Card */}
      <div className="bg-surface border border-subtle rounded-xl p-5 shadow-xl">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-subtle">
          <div>
            <div className="flex items-center gap-2 text-xs font-mono text-slate-400 mb-1">
              <span>STAN SILNIKA:</span>
              <span className="font-bold text-sky-400 uppercase">
                {run?.engine?.status || "GOTOWY DO PRACY"}
              </span>
              <span className="text-slate-600">•</span>
              <span>POLITYKA:</span>
              <span className="text-indigo-300 font-semibold uppercase">{activePolicyPreset}</span>
            </div>
            <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
              <span>{run?.project || "AAW Autonomous Coding Engine"}</span>
            </h2>
            <p className="text-xs text-slate-400 mt-1 max-w-2xl">
              {run?.goal ||
                "Wybierz zadanie z listy lub skonfiguruj nowy cel w New Task Wizard, aby uruchomić 7-fazowy proces orkiestracji."}
            </p>
          </div>

          <div className="flex items-center gap-2 self-start sm:self-center shrink-0">
            <button
              onClick={() => loadBootstrap()}
              disabled={loading}
              className="px-3 py-1.5 bg-elevated hover:bg-slate-700 text-xs font-medium text-slate-200 border border-subtle rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin text-sky-400" : ""}`} />
              <span>Odśwież dane</span>
            </button>
            <button
              onClick={() => (pollingActive ? stopPolling() : startPolling())}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg border flex items-center gap-1.5 transition ${
                pollingActive
                  ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/20"
                  : "bg-elevated border-subtle text-slate-300 hover:text-white"
              }`}
            >
              <Radio className={`w-3.5 h-3.5 ${pollingActive ? "animate-pulse" : ""}`} />
              <span>{pollingActive ? "Zatrzymaj Polling" : "Włącz Polling"}</span>
            </button>
          </div>
        </div>

        {/* Quick Stats Grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-4">
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1">
              Aktywny Run ID
            </div>
            <div className="text-xs font-mono font-bold text-slate-200 truncate">
              {activeRunId || (run?.run_id ? run.run_id : "None")}
            </div>
          </div>
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1">
              Iteracja
            </div>
            <div className="text-xs font-mono font-bold text-slate-200">
              {run?.process?.iteration || 0} / {run?.process?.max_iterations || 5}
            </div>
          </div>
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1">
              Czas działania
            </div>
            <div className="text-xs font-mono font-bold text-slate-200">
              {run?.process?.run_elapsed_s ? `${run.process.run_elapsed_s}s` : "0s"}
            </div>
          </div>
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1">
              Bramka Human-in-Loop
            </div>
            <div className="flex items-center gap-1.5 text-xs font-semibold text-gate">
              <ShieldAlert className="w-3.5 h-3.5 text-gate" />
              <span>Aktywna i bezpieczna</span>
            </div>
          </div>
        </div>
      </div>

      {/* 7-Phase Execution Graph with Animated State Machine */}
      <PhaseGraph run={run} />

      {/* Human Gate Decision Modal */}
      <GateDecisionModal
        isOpen={gateModalMode !== null}
        mode={gateModalMode}
        run={run}
        onClose={() => setGateModalMode(null)}
      />
    </div>
  );

  return (
    <StudioLayout
      leftRail={<LeftRail />}
      centerCanvas={centerCanvas}
      rightDrawer={<RightDrawer />}
    />
  );
}

export default App;
