/**
 * AAW Agentic Studio — Live Command Center View
 *
 * Primary execution monitoring view:
 * - Active run hero card with engine status, policy preset, and quick stats
 * - Real execution controls: START, STOP SAFELY, STOP NOW, RESUME, REFRESH, POLLING
 * - Sticky HumanGateBanner for hold states with GateDecisionModal (ACCEPT, REJECT, CONTINUE)
 * - 7-Phase Execution Graph with state transitions and telemetry
 */

import React, { useState } from "react";
import {
  ShieldAlert,
  RefreshCw,
  Radio,
  Play,
  Square,
  AlertOctagon,
  PlusCircle,
  Clock,
  Repeat,
  AlertTriangle,
} from "lucide-react";
import { DecisionAction } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";
import { PhaseGraph } from "../graph/PhaseGraph";
import { HumanGateBanner } from "../gate/HumanGateBanner";
import { GateDecisionModal } from "../gate/GateDecisionModal";
import { LiveChainPanel } from "../live/LiveChainPanel";

export const LiveCommandCenterView: React.FC = () => {
  const {
    activeRunId,
    run,
    loading,
    error,
    pollingActive,
    activePolicyPreset,
    loadBootstrap,
    refreshActiveRun,
    clearError,
    startPolling,
    stopPolling,
    stopRun,
    resumeRun,
    setActiveNav,
  } = useEngineStore();

  const [gateModalMode, setGateModalMode] = useState<DecisionAction | null>(null);
  const [stoppingAction, setStoppingAction] = useState<string | null>(null);

  const engineStatus = run?.engine?.status || run?.status || "GOTOWY DO PRACY";
  const isRunning =
    run?.status === "RUNNING" ||
    run?.engine?.status === "RUNNING" ||
    run?.engine?.status === "EXECUTE" ||
    run?.engine?.status === "PLAN" ||
    run?.engine?.status === "REPAIR";

  const isPausedOrInterrupted =
    run?.status === "PAUSED" ||
    run?.status === "INTERRUPTED" ||
    (run?.status as string) === "HALTED_BY_USER";

  const handleStopSafely = async () => {
    setStoppingAction("safe");
    try {
      await stopRun(false);
    } finally {
      setStoppingAction(null);
    }
  };

  const handleStopForce = async () => {
    setStoppingAction("force");
    try {
      await stopRun(true);
    } finally {
      setStoppingAction(null);
    }
  };

  const handleResume = async () => {
    setStoppingAction("resume");
    try {
      await resumeRun();
    } finally {
      setStoppingAction(null);
    }
  };

  return (
    <div className="max-w-5xl w-full mx-auto space-y-6 animate-fade-in" data-testid="live-command-center-view">
      {/* Error notification banner */}
      {error && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-xl flex items-center justify-between text-rose-300 text-xs shadow-lg">
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
              <span className="font-bold text-sky-400 uppercase tracking-wider">
                {engineStatus}
              </span>
              <span className="text-slate-600">•</span>
              <span>POLITYKA:</span>
              <span className="text-indigo-300 font-semibold uppercase">{activePolicyPreset}</span>
            </div>
            <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
              <span>{run?.project || "AAW Autonomous Coding Engine"}</span>
            </h2>
            <p className="text-xs text-slate-400 mt-1 max-w-2xl leading-relaxed">
              {run?.goal ||
                "Brak aktywnego zadania. Utwórz nowy cel w kreatorze zadań lub wybierz archiwalny run z historii, aby monitorować postęp 7 faz orkiestracji."}
            </p>
          </div>

          {/* Top action button cluster */}
          <div className="flex flex-wrap items-center gap-2 self-start sm:self-center shrink-0">
            {/* Start / New Task Button */}
            <button
              onClick={() => setActiveNav("new-task")}
              className="px-3 py-1.5 bg-sky-600 hover:bg-sky-500 text-xs font-medium text-white rounded-lg flex items-center gap-1.5 shadow transition"
              data-testid="nav-new-task-btn"
            >
              <PlusCircle className="w-3.5 h-3.5" />
              <span>Nowe Zadanie</span>
            </button>

            {/* Stop Safely */}
            {isRunning && (
              <button
                onClick={handleStopSafely}
                disabled={stoppingAction !== null}
                className="px-3 py-1.5 bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 text-xs font-medium rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
                title="Dokończ bieżącą iterację i bezpiecznie zatrzymaj silnik"
                data-testid="stop-safely-btn"
              >
                <Square className="w-3.5 h-3.5 text-amber-400" />
                <span>{stoppingAction === "safe" ? "Zatrzymywanie..." : "Stop Bezpieczny"}</span>
              </button>
            )}

            {/* Stop Force / Immediately */}
            {isRunning && (
              <button
                onClick={handleStopForce}
                disabled={stoppingAction !== null}
                className="px-3 py-1.5 bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/40 text-xs font-medium rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
                title="Wymuś natychmiastowe zatrzymanie procesu (SIGTERM/SIGKILL)"
                data-testid="stop-force-btn"
              >
                <AlertOctagon className="w-3.5 h-3.5 text-rose-400" />
                <span>{stoppingAction === "force" ? "Wymuszanie..." : "Stop Teraz"}</span>
              </button>
            )}

            {/* Resume Task */}
            {isPausedOrInterrupted && (
              <button
                onClick={handleResume}
                disabled={stoppingAction !== null}
                className="px-3 py-1.5 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 border border-emerald-500/40 text-xs font-medium rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
                title="Wznów wykonywanie przerwanego lub zatrzymanego zadania"
                data-testid="resume-btn"
              >
                <Play className="w-3.5 h-3.5 text-emerald-400" />
                <span>{stoppingAction === "resume" ? "Wznawianie..." : "Wznów Zadanie"}</span>
              </button>
            )}

            {/* Refresh */}
            <button
              onClick={() => {
                loadBootstrap();
                refreshActiveRun();
              }}
              disabled={loading}
              className="px-3 py-1.5 bg-elevated hover:bg-slate-700 text-xs font-medium text-slate-200 border border-subtle rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
              title="Pobierz najświeższy stan z serwera"
              data-testid="refresh-btn"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin text-sky-400" : ""}`} />
              <span>Odśwież</span>
            </button>

            {/* Polling Toggle */}
            <button
              onClick={() => (pollingActive ? stopPolling() : startPolling())}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg border flex items-center gap-1.5 transition ${
                pollingActive
                  ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/20"
                  : "bg-elevated border-subtle text-slate-300 hover:text-white"
              }`}
              data-testid="polling-toggle-btn"
            >
              <Radio className={`w-3.5 h-3.5 ${pollingActive ? "animate-pulse" : ""}`} />
              <span>{pollingActive ? "Polling aktywny" : "Włącz Polling"}</span>
            </button>
          </div>
        </div>

        {/* Quick Stats Grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-4">
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1 flex items-center gap-1.5">
              <span>Aktywny Run ID</span>
            </div>
            <div className="text-xs font-mono font-bold text-slate-200 truncate" title={activeRunId || run?.run_id || ""}>
              {activeRunId || (run?.run_id ? run.run_id : "Brak")}
            </div>
          </div>
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1 flex items-center gap-1.5">
              <Repeat className="w-3 h-3 text-slate-400" />
              <span>Iteracja</span>
            </div>
            <div className="text-xs font-mono font-bold text-slate-200">
              {run?.process?.iteration || 0} / {run?.process?.max_iterations || 5}
            </div>
          </div>
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1 flex items-center gap-1.5">
              <Clock className="w-3 h-3 text-slate-400" />
              <span>Czas działania</span>
            </div>
            <div className="text-xs font-mono font-bold text-slate-200">
              {run?.process?.run_elapsed_s ? `${run.process.run_elapsed_s}s` : "0s"}
            </div>
          </div>
          <div className="bg-elevated/40 p-3 rounded-lg border border-subtle">
            <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 mb-1 flex items-center gap-1.5">
              <ShieldAlert className="w-3 h-3 text-amber-400" />
              <span>Bramka Human-in-Loop</span>
            </div>
            <div className="flex items-center gap-1.5 text-xs font-semibold text-gate">
              <span>{run?.gate?.why ? "Wymagana (Bezpieczna)" : "Aktywna i bezpieczna"}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Live chain panel (roadmap 2.3): journey, chain slots, meter, forecast, feed */}
      {(run as any)?.live && <LiveChainPanel live={(run as any).live} status={run?.status} />}

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
};
