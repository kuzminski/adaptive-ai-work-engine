/**
 * AAW Agentic Studio — Phase Node Component
 *
 * Renders an individual engine phase card within the 7-phase execution graph.
 * Displays phase index, name, Polish sublabel, status badge, duration, and icon.
 * Supports active glow, human gate amber pulse, and repair loop styling.
 */

import {
  CheckCircle2,
  Clock,
  Loader2,
  ShieldAlert,
  AlertTriangle,
  RotateCcw,
} from "lucide-react";
import { EnginePhase } from "../../api/types";

export type PhaseStatus =
  | "pending"
  | "active"
  | "passed"
  | "failed"
  | "waiting_gate"
  | "looping";

export interface PhaseNodeInfo {
  index: number; // 1 to 7
  key: EnginePhase | string;
  name: string;
  sublabel: string; // e.g. "Dekompozycja", "Kodowanie", etc.
  description?: string;
  status: PhaseStatus;
  duration?: string | null;
  iterationCount?: number;
  isHumanGate?: boolean;
  colorHex: string;
  borderColor: string;
  bgActiveColor: string;
  textColor?: string;
}

export interface PhaseNodeProps {
  phase: PhaseNodeInfo;
  isActive?: boolean;
  onClick?: () => void;
  className?: string;
}

export function PhaseNode({ phase, isActive, onClick, className = "" }: PhaseNodeProps) {
  const isNodeActive = isActive ?? (phase.status === "active" || phase.status === "looping");
  const isGate = phase.isHumanGate || phase.status === "waiting_gate";

  // Formatted two-digit index (e.g. 01, 02)
  const formattedIndex = String(phase.index).padStart(2, "0");

  // Determine card border, background, and glow classes based on state
  let cardStateClasses = "border-subtle bg-elevated/40 text-slate-400 hover:border-slate-700";

  if (isGate) {
    cardStateClasses =
      "border-gate bg-gate/10 text-amber-200 ring-2 ring-gate/50 shadow-[0_0_18px_rgba(245,158,11,0.35)] animate-pulse";
  } else if (phase.status === "looping") {
    cardStateClasses =
      "border-phase-repair bg-phase-repair/15 text-rose-200 ring-1 ring-phase-repair/60 shadow-[0_0_15px_rgba(244,63,94,0.3)] animate-pulse";
  } else if (isNodeActive) {
    cardStateClasses = `${phase.borderColor} bg-elevated/90 text-white ring-1 ring-sky-400/60 shadow-[0_0_15px_rgba(56,189,248,0.25)]`;
  } else if (phase.status === "passed") {
    cardStateClasses = "border-subtle/80 bg-elevated/60 text-slate-200 hover:border-slate-600";
  } else if (phase.status === "failed") {
    cardStateClasses = "border-rose-500/60 bg-rose-500/10 text-rose-300 ring-1 ring-rose-500/40";
  }

  return (
    <div
      data-testid={`phase-node-${phase.key.toLowerCase()}`}
      onClick={onClick}
      className={`relative p-3.5 rounded-xl border flex flex-col justify-between transition-all duration-200 select-none ${cardStateClasses} ${className}`}
      style={{
        borderLeftWidth: isNodeActive || isGate ? "4px" : undefined,
        borderLeftColor: isGate ? "#f59e0b" : isNodeActive ? phase.colorHex : undefined,
      }}
    >
      {/* Top Header: Index & Status Icon */}
      <div className="flex items-center justify-between text-xs font-mono mb-2">
        <span
          className="font-bold tracking-wider text-[11px]"
          style={{ color: isGate ? "#f59e0b" : isNodeActive ? phase.colorHex : "#94a3b8" }}
        >
          {formattedIndex}
        </span>

        <div className="flex items-center gap-1.5">
          {isGate && (
            <ShieldAlert className="w-4 h-4 text-gate animate-bounce" data-testid="icon-gate" />
          )}
          {phase.status === "looping" && (
            <RotateCcw className="w-3.5 h-3.5 text-phase-repair animate-spin" data-testid="icon-loop" />
          )}
          {phase.status === "active" && !isGate && (
            <span className="flex h-2 w-2 relative" data-testid="icon-active-pulse">
              <span
                className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75"
                style={{ backgroundColor: phase.colorHex }}
              />
              <span
                className="relative inline-flex rounded-full h-2 w-2"
                style={{ backgroundColor: phase.colorHex }}
              />
            </span>
          )}
          {phase.status === "passed" && (
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" data-testid="icon-passed" />
          )}
          {phase.status === "failed" && (
            <AlertTriangle className="w-3.5 h-3.5 text-rose-400" data-testid="icon-failed" />
          )}
          {phase.status === "pending" && (
            <Clock className="w-3 h-3 text-slate-600" data-testid="icon-pending" />
          )}
        </div>
      </div>

      {/* Middle Body: Phase Name & Polish Sublabel */}
      <div className="space-y-1 mb-3">
        <div
          className="text-xs font-mono font-bold tracking-tight truncate flex items-center gap-1.5"
          style={{ color: isGate ? "#fbbf24" : isNodeActive ? "#ffffff" : "#e2e8f0" }}
          title={phase.name}
        >
          <span>{phase.name}</span>
        </div>
        <div
          className="text-[11px] text-slate-400 truncate leading-tight font-sans"
          data-testid={`phase-sublabel-${phase.key.toLowerCase()}`}
          title={phase.sublabel}
        >
          {phase.sublabel}
        </div>
      </div>

      {/* Bottom Footer: Status Badge & Duration / Loop Info */}
      <div className="pt-2 border-t border-subtle/50 flex items-center justify-between text-[10px] font-mono">
        <div data-testid={`phase-status-${phase.key.toLowerCase()}`}>
          {isGate ? (
            <span className="px-1.5 py-0.5 rounded font-bold bg-gate/20 text-amber-300 border border-gate/40 animate-pulse flex items-center gap-1">
              BRAMKA CZŁOWIEKA
            </span>
          ) : phase.status === "looping" ? (
            <span className="px-1.5 py-0.5 rounded font-bold bg-phase-repair/20 text-rose-300 border border-phase-repair/40 flex items-center gap-1">
              NAPRAWA
            </span>
          ) : phase.status === "active" ? (
            <span
              className="px-1.5 py-0.5 rounded font-bold border flex items-center gap-1"
              style={{
                backgroundColor: `${phase.colorHex}20`,
                borderColor: `${phase.colorHex}50`,
                color: phase.colorHex,
              }}
            >
              <Loader2 className="w-2.5 h-2.5 animate-spin" />
              AKTYWNY
            </span>
          ) : phase.status === "passed" ? (
            <span className="px-1.5 py-0.5 rounded font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
              ZALICZONY
            </span>
          ) : phase.status === "failed" ? (
            <span className="px-1.5 py-0.5 rounded font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/40">
              BŁĄD
            </span>
          ) : (
            <span className="px-1.5 py-0.5 rounded font-medium bg-elevated/50 text-slate-500 border border-subtle">
              OCZEKUJE
            </span>
          )}
        </div>

        {/* Duration / Iteration pill */}
        <div className="text-slate-400 font-mono text-[9px]">
          {phase.duration ? (
            <span>{phase.duration}</span>
          ) : phase.iterationCount && phase.iterationCount > 0 ? (
            <span className="text-rose-300 font-bold">{`#${phase.iterationCount}`}</span>
          ) : null}
        </div>
      </div>
    </div>
  );
}
