import React from "react";
import ReactDOM from "react-dom/client";
import "./tokens.css";
import { Cpu, Terminal, ShieldAlert, CheckCircle2 } from "lucide-react";

export function App() {
  const token =
    typeof document !== "undefined"
      ? document.querySelector('meta[name="aaw-token"]')?.getAttribute("content") || "none"
      : "none";

  const phases = [
    { name: "PLAN", color: "bg-phase-plan text-indigo-950" },
    { name: "EXECUTE", color: "bg-phase-execute text-sky-950" },
    { name: "SELF_VERIFY", color: "bg-phase-self_verify text-purple-950" },
    { name: "REVIEW", color: "bg-phase-review text-purple-950" },
    { name: "REPAIR", color: "bg-phase-repair text-rose-950" },
    { name: "FINAL_REVIEW", color: "bg-phase-final_review text-blue-950" },
    { name: "ROADMAP_CHECK", color: "bg-phase-roadmap_check text-emerald-950" },
  ];

  return (
    <div className="min-h-screen bg-base text-slate-100 p-8 flex flex-col items-center justify-center font-sans">
      <div className="max-w-2xl w-full bg-surface border border-subtle rounded-xl p-8 shadow-2xl">
        <div className="flex items-center gap-3 mb-6">
          <div className="p-2.5 bg-elevated rounded-lg border border-subtle text-phase-execute">
            <Cpu className="w-6 h-6" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-white">AAW Agentic Studio</h1>
            <p className="text-sm text-slate-400">Command Center Scaffolding & Design System Smoke Test</p>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4 mb-6">
          <div className="bg-elevated p-4 rounded-lg border border-subtle">
            <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold mb-1">Theme Surface</div>
            <div className="text-sm text-slate-200">Cyber-Dark Palette Active</div>
          </div>
          <div className="bg-elevated p-4 rounded-lg border border-subtle">
            <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold mb-1">Human Gate Status</div>
            <div className="flex items-center gap-2 text-gate font-semibold text-sm">
              <ShieldAlert className="w-4 h-4 text-gate" />
              <span>Gate Ready (#f59e0b)</span>
            </div>
          </div>
        </div>

        <div className="mb-6">
          <div className="text-xs uppercase tracking-wider text-slate-400 font-semibold mb-2">Engine Execution Phases</div>
          <div className="flex flex-wrap gap-2">
            {phases.map((p) => (
              <span key={p.name} className={`px-2.5 py-1 text-xs font-mono font-bold rounded ${p.color}`}>
                {p.name}
              </span>
            ))}
          </div>
        </div>

        <div className="bg-terminal p-4 rounded-lg border border-subtle font-mono text-xs text-slate-300">
          <div className="flex items-center gap-2 text-slate-400 mb-2">
            <Terminal className="w-4 h-4" />
            <span>Loopback Token Check</span>
          </div>
          <div className="text-emerald-400 flex items-center gap-1.5">
            <CheckCircle2 className="w-3.5 h-3.5" />
            <span>Token Target: {token}</span>
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
