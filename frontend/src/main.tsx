import React, { useEffect } from "react";
import ReactDOM from "react-dom/client";
import "./tokens.css";
import {
  Cpu,
  Terminal,
  ShieldAlert,
  CheckCircle2,
  RefreshCw,
  FileCode,
  Sliders,
  Layers,
  Radio,
  AlertTriangle,
} from "lucide-react";
import { client } from "./api/client";
import { DrawerTab, RoleFilter } from "./api/types";
import { useEngineStore } from "./store/engineStore";

export function App() {
  const {
    bootstrap,
    activeRunId,
    run,
    mcpStatus,
    loading,
    error,
    selectedTab,
    roleFilter,
    pollingActive,
    loadBootstrap,
    loadMCPStatus,
    setSelectedTab,
    setRoleFilter,
    clearError,
  } = useEngineStore();

  const token = client.getToken() || "none";

  useEffect(() => {
    // Attempt automatic bootstrap on mount if served by AAW server
    if (token && token !== "none" && token !== "__AAW_TOKEN__") {
      loadBootstrap();
    }
  }, [token, loadBootstrap]);

  const phases = [
    { name: "PLAN", color: "bg-phase-plan text-indigo-950" },
    { name: "EXECUTE", color: "bg-phase-execute text-sky-950" },
    { name: "SELF_VERIFY", color: "bg-phase-self_verify text-purple-950" },
    { name: "REVIEW", color: "bg-phase-review text-purple-950" },
    { name: "REPAIR", color: "bg-phase-repair text-rose-950" },
    { name: "FINAL_REVIEW", color: "bg-phase-final_review text-blue-950" },
    { name: "ROADMAP_CHECK", color: "bg-phase-roadmap_check text-emerald-950" },
  ];

  const tabs: DrawerTab[] = ["terminal", "diff", "evidence"];
  const roles: RoleFilter[] = ["All", "Planner", "Implementer", "Verifier", "System"];

  return (
    <div className="min-h-screen bg-base text-slate-100 p-8 flex flex-col items-center justify-center font-sans">
      <div className="max-w-3xl w-full bg-surface border border-subtle rounded-xl p-8 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between mb-6 pb-4 border-b border-subtle">
          <div className="flex items-center gap-3">
            <div className="p-2.5 bg-elevated rounded-lg border border-subtle text-phase-execute">
              <Cpu className="w-6 h-6" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-white">AAW Agentic Studio</h1>
              <p className="text-sm text-slate-400">Task 3: Typed API Client & Polling Engine Store Live</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span
              className={`px-2.5 py-1 text-xs font-mono font-bold rounded-full flex items-center gap-1.5 ${
                pollingActive
                  ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/30"
                  : "bg-slate-800 text-slate-400 border border-slate-700"
              }`}
            >
              <Radio className={`w-3 h-3 ${pollingActive ? "animate-pulse" : ""}`} />
              {pollingActive ? "POLLING ACTIVE (1s)" : "POLLING IDLE"}
            </span>
          </div>
        </div>

        {/* Error notification banner */}
        {error && (
          <div className="mb-6 p-4 bg-rose-500/10 border border-rose-500/30 rounded-lg flex items-center justify-between text-rose-300 text-sm">
            <div className="flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 text-rose-400" />
              <span>{error}</span>
            </div>
            <button
              onClick={clearError}
              className="text-xs px-2 py-1 bg-rose-500/20 hover:bg-rose-500/30 rounded text-rose-200 transition"
            >
              Dismiss
            </button>
          </div>
        )}

        {/* Status Grid */}
        <div className="grid grid-cols-3 gap-4 mb-6">
          <div className="bg-elevated p-4 rounded-lg border border-subtle">
            <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold mb-1">Engine Store</div>
            <div className="text-sm text-slate-200 flex items-center gap-1.5">
              <CheckCircle2 className="w-4 h-4 text-emerald-400" />
              <span>useEngineStore Ready</span>
            </div>
          </div>
          <div className="bg-elevated p-4 rounded-lg border border-subtle">
            <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold mb-1">Active Run</div>
            <div className="text-sm font-mono text-slate-300 truncate">
              {activeRunId || (run?.run_id ? run.run_id : "None Selected")}
            </div>
          </div>
          <div className="bg-elevated p-4 rounded-lg border border-subtle">
            <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold mb-1">Human Gate Status</div>
            <div className="flex items-center gap-2 text-gate font-semibold text-sm">
              <ShieldAlert className="w-4 h-4 text-gate" />
              <span>Gate Aware</span>
            </div>
          </div>
        </div>

        {/* Interactive Store State Controls */}
        <div className="mb-6 bg-elevated/50 p-4 rounded-lg border border-subtle">
          <div className="flex items-center gap-2 mb-3 text-xs font-semibold uppercase tracking-wider text-slate-400">
            <Sliders className="w-4 h-4 text-phase-execute" />
            <span>Interactive Store Actions</span>
          </div>

          <div className="grid grid-cols-2 gap-4 mb-4">
            {/* Drawer Tab Switcher */}
            <div>
              <div className="text-xs text-slate-400 mb-1.5 flex items-center gap-1">
                <FileCode className="w-3.5 h-3.5" />
                <span>Active Drawer Tab:</span>
                <span className="font-mono text-sky-400 font-bold ml-1">{selectedTab}</span>
              </div>
              <div className="flex gap-1.5">
                {tabs.map((t) => (
                  <button
                    key={t}
                    onClick={() => setSelectedTab(t)}
                    className={`px-3 py-1.5 text-xs font-medium rounded transition ${
                      selectedTab === t
                        ? "bg-phase-execute text-sky-950 font-bold"
                        : "bg-surface hover:bg-elevated text-slate-300 border border-subtle"
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>

            {/* Role Filter Switcher */}
            <div>
              <div className="text-xs text-slate-400 mb-1.5 flex items-center gap-1">
                <Layers className="w-3.5 h-3.5" />
                <span>Role Filter:</span>
                <span className="font-mono text-indigo-400 font-bold ml-1">{roleFilter}</span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {roles.map((r) => (
                  <button
                    key={r}
                    onClick={() => setRoleFilter(r)}
                    className={`px-2.5 py-1 text-xs font-medium rounded transition ${
                      roleFilter === r
                        ? "bg-phase-plan text-indigo-950 font-bold"
                        : "bg-surface hover:bg-elevated text-slate-300 border border-subtle"
                    }`}
                  >
                    {r}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Action Triggers */}
          <div className="flex items-center gap-2 pt-2 border-t border-subtle/50">
            <button
              onClick={() => loadBootstrap()}
              disabled={loading}
              className="px-3 py-1.5 bg-surface hover:bg-elevated text-xs font-medium text-slate-200 border border-subtle rounded flex items-center gap-1.5 transition disabled:opacity-50"
            >
              <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} />
              <span>Load Bootstrap</span>
            </button>

            <button
              onClick={() => loadMCPStatus()}
              className="px-3 py-1.5 bg-surface hover:bg-elevated text-xs font-medium text-slate-200 border border-subtle rounded transition"
            >
              Check MCP Hub ({mcpStatus ? "Ready" : "Unloaded"})
            </button>
          </div>
        </div>

        {/* 7-Phase Graph Pills */}
        <div className="mb-6">
          <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold mb-2">
            Engine Execution Phases (7-Phase Machine)
          </div>
          <div className="flex flex-wrap gap-2">
            {phases.map((p) => (
              <span key={p.name} className={`px-2.5 py-1 text-xs font-mono font-bold rounded ${p.color}`}>
                {p.name}
              </span>
            ))}
          </div>
        </div>

        {/* Diagnostics Terminal */}
        <div className="bg-terminal p-4 rounded-lg border border-subtle font-mono text-xs text-slate-300 space-y-1.5">
          <div className="flex items-center gap-2 text-slate-400 pb-1 mb-1 border-b border-subtle/50">
            <Terminal className="w-4 h-4" />
            <span>Client & Store Runtime Telemetry</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-slate-400">Loopback X-AAW-Token:</span>
            <span className="text-emerald-400">{token.substring(0, 16)}...</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-slate-400">Backend App Version:</span>
            <span className="text-sky-400">{bootstrap?.version || "Awaiting bootstrap"}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-slate-400">Store Loading State:</span>
            <span className="text-amber-400">{loading ? "BUSY" : "IDLE"}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

const rootElement = document.getElementById("root");
if (rootElement) {
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}
