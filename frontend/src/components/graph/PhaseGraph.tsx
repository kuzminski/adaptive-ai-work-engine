/**
 * AAW Agentic Studio — 7-Phase Execution Graph
 *
 * Renders AAW's 7 engine phases in horizontal order:
 * 1. PLAN -> 2. EXECUTE -> 3. SELF_VERIFY -> 4. REVIEW ⮂ 5. REPAIR -> 6. FINAL_REVIEW -> 7. ROADMAP_CHECK.
 *
 * Features:
 * - Distinctive SVG loopback curve for REPAIR connecting REVIEW -> REPAIR -> SELF_VERIFY
 * - Real-time state resolution mapping RunDetail to active/passed/pending/waiting states
 * - High-visibility amber pulsing human gate at Node 7 when engine is AWAITING_HUMAN
 * - Synchronized event telemetry bar underneath displaying current activity, role, and model
 */

import {
  RotateCcw,
  ShieldAlert,
  User,
  Cpu,
  Activity,
} from "lucide-react";
import { EnginePhase, RunDetail } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";
import { PhaseNode, PhaseNodeInfo, PhaseStatus } from "./PhaseNode";

export interface PhaseGraphProps {
  run?: RunDetail | null;
  className?: string;
  onPhaseSelect?: (phaseKey: string) => void;
}

export const PHASE_DEFINITIONS: Array<{
  index: number;
  key: EnginePhase;
  name: string;
  sublabel: string;
  description: string;
  colorHex: string;
  borderColor: string;
  bgActiveColor: string;
}> = [
  {
    index: 1,
    key: "PLAN",
    name: "PLAN",
    sublabel: "Dekompozycja",
    description: "Analiza celów, dekompozycja zadania i strategia",
    colorHex: "#818cf8",
    borderColor: "border-phase-plan",
    bgActiveColor: "bg-phase-plan/10",
  },
  {
    index: 2,
    key: "EXECUTE",
    name: "EXECUTE",
    sublabel: "Kodowanie",
    description: "Generowanie kodu, modyfikacje i implementacja",
    colorHex: "#38bdf8",
    borderColor: "border-phase-execute",
    bgActiveColor: "bg-phase-execute/10",
  },
  {
    index: 3,
    key: "SELF_VERIFY",
    name: "SELF_VERIFY",
    sublabel: "Testy w roli",
    description: "Autonomiczna weryfikacja implementera i testy",
    colorHex: "#c084fc",
    borderColor: "border-phase-self_verify",
    bgActiveColor: "bg-phase-self_verify/10",
  },
  {
    index: 4,
    key: "REVIEW",
    name: "REVIEW",
    sublabel: "Recenzja",
    description: "Niezależny przegląd jakości, bezpieczeństwa i spójności",
    colorHex: "#a78bfa",
    borderColor: "border-phase-review",
    bgActiveColor: "bg-phase-review/10",
  },
  {
    index: 5,
    key: "REPAIR",
    name: "REPAIR",
    sublabel: "Pętla napraw",
    description: "Korekta defektów i ponowna weryfikacja (loopback)",
    colorHex: "#f43f5e",
    borderColor: "border-phase-repair",
    bgActiveColor: "bg-phase-repair/10",
  },
  {
    index: 6,
    key: "FINAL_REVIEW",
    name: "FINAL_REVIEW",
    sublabel: "Całościowa ocena",
    description: "Ostateczna ocena kryteriów akceptacji i regresji",
    colorHex: "#3b82f6",
    borderColor: "border-phase-final_review",
    bgActiveColor: "bg-phase-final_review/10",
  },
  {
    index: 7,
    key: "ROADMAP_CHECK",
    name: "ROADMAP_CHECK",
    sublabel: "Weryfikacja celu",
    description: "Ocena realizacji celów roadmapy i bramka decyzyjna",
    colorHex: "#34d399",
    borderColor: "border-phase-roadmap_check",
    bgActiveColor: "bg-phase-roadmap_check/10",
  },
];

/**
 * Pure state machine resolution helper mapping RunDetail to 7-phase statuses
 */
export function resolvePhaseNodes(run: RunDetail | null): {
  phases: PhaseNodeInfo[];
  currentPhaseKey: string | null;
  repairCount: number;
  isRepairActive: boolean;
  isAwaitingHuman: boolean;
  lastActivityText: string;
  activeRole: string;
  activeModel: string;
  iterationText: string;
  elapsedText: string;
} {
  // Extract phase from run details (supporting current_step, process.phase, engine.phase)
  const rawPhase =
    (run as any)?.current_step ||
    run?.process?.phase ||
    run?.engine?.phase ||
    (run as any)?.phase ||
    (run as any)?.state;

  const currentPhaseKey: string | null = rawPhase ? String(rawPhase).toUpperCase() : null;

  // Determine state flags
  const isAwaitingHuman =
    run?.engine?.status === "AWAITING_HUMAN" ||
    run?.status === "READY_FOR_DECISION" ||
    run?.status === "NEEDS_ATTENTION" ||
    (run as any)?.state === "AWAITING_HUMAN" ||
    (Boolean(run?.gate) && !(run?.gate as any)?.decision);

  const isAccepted =
    run?.status === "ACCEPTED" ||
    run?.engine?.status === "HUMAN_APPROVED" ||
    run?.engine?.status === "PROMOTED";

  const isFailed =
    run?.status === "REJECTED" ||
    run?.engine?.status === "REJECTED" ||
    run?.status === "START_FAILED";

  const repairCount =
    run?.process?.standing?.iterations ||
    (run as any)?.iterations ||
    (run as any)?.repair_count ||
    0;

  const isRepairActive =
    currentPhaseKey === "REPAIR" || (run as any)?.state === "REPAIR";

  // Map each phase definition to resolved status
  const curIdx = PHASE_DEFINITIONS.findIndex((p) => p.key === currentPhaseKey);

  const phases: PhaseNodeInfo[] = PHASE_DEFINITIONS.map((def, idx) => {
    let status: PhaseStatus = "pending";
    let isHumanGate = false;
    let duration: string | null = null;
    let iterationCount: number | undefined = undefined;

    if (!run || run.status === "STARTING") {
      status = "pending";
    } else if (isAccepted) {
      status = "passed";
    } else if (isAwaitingHuman) {
      // Node 7 (or gate node) transitions to human gate hold
      if (idx === 6) {
        status = "waiting_gate";
        isHumanGate = true;
      } else if (curIdx >= 0 && idx <= curIdx) {
        status = "passed";
      } else {
        status = "passed";
      }
    } else if (isFailed) {
      if (curIdx >= 0 && idx < curIdx) {
        status = "passed";
      } else if (curIdx >= 0 && idx === curIdx) {
        status = "failed";
      } else {
        status = "pending";
      }
    } else if (isRepairActive) {
      if (def.key === "REPAIR") {
        status = "looping";
        iterationCount = repairCount > 0 ? repairCount : 1;
      } else if (idx < 4) {
        // Phases PLAN (0), EXECUTE (1), SELF_VERIFY (2), REVIEW (3) have completed
        status = "passed";
      } else {
        status = "pending";
      }
    } else if (curIdx >= 0) {
      if (idx < curIdx) {
        status = "passed";
      } else if (idx === curIdx) {
        status = "active";
        if (run?.process?.phase_elapsed_s) {
          duration = `${run.process.phase_elapsed_s}s`;
        }
      } else {
        status = "pending";
      }
    }

    return {
      index: def.index,
      key: def.key,
      name: def.name,
      sublabel: def.sublabel,
      description: def.description,
      status,
      duration,
      iterationCount,
      isHumanGate,
      colorHex: def.colorHex,
      borderColor: def.borderColor,
      bgActiveColor: def.bgActiveColor,
    };
  });

  // Telemetry metadata
  const lastActivityText =
    run?.process?.activity ||
    run?.last_activity ||
    (run?.goal
      ? `Zadanie w toku: ${run.goal}`
      : "Oczekiwanie na uruchomienie zadania silnika AAW...");

  const activeRole =
    run?.process?.role ||
    (currentPhaseKey === "PLAN"
      ? "Planner"
      : currentPhaseKey === "EXECUTE" || currentPhaseKey === "REPAIR"
      ? "Implementer"
      : currentPhaseKey === "SELF_VERIFY" ||
        currentPhaseKey === "REVIEW" ||
        currentPhaseKey === "FINAL_REVIEW"
      ? "Verifier"
      : "System");

  const activeModel =
    run?.process?.model ||
    (run?.engine?.policy_preset
      ? `Polityka: ${run.engine.policy_preset}`
      : "AAW Multi-Model");

  const iteration = run?.process?.iteration || 0;
  const maxIterations = run?.process?.max_iterations || 5;
  const iterationText = `Iteracja ${iteration} / ${maxIterations}`;

  const phaseElapsed = run?.process?.phase_elapsed_s ? `${run.process.phase_elapsed_s}s` : "0s";
  const runElapsed = run?.process?.run_elapsed_s ? `${run.process.run_elapsed_s}s` : "0s";
  const elapsedText = `Faza: ${phaseElapsed} • Przebieg: ${runElapsed}`;

  return {
    phases,
    currentPhaseKey,
    repairCount,
    isRepairActive,
    isAwaitingHuman,
    lastActivityText,
    activeRole,
    activeModel,
    iterationText,
    elapsedText,
  };
}

export function PhaseGraph({ run: propRun, className = "", onPhaseSelect }: PhaseGraphProps) {
  const store = useEngineStore();
  const run = propRun !== undefined ? propRun : store.run;

  const {
    phases,
    repairCount,
    isRepairActive,
    isAwaitingHuman,
    lastActivityText,
    activeRole,
    activeModel,
    iterationText,
    elapsedText,
  } = resolvePhaseNodes(run);

  return (
    <div
      className={`bg-surface border border-subtle rounded-xl p-5 shadow-xl relative animate-fade-in ${className}`}
      data-testid="phase-graph-container"
    >
      {/* Graph Header: Title, Description & Status Badges */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 pb-3 border-b border-subtle">
        <div>
          <h3 className="text-sm font-bold text-slate-100 uppercase tracking-wider flex items-center gap-2">
            <span>7-Fazowa Maszyna Stanów (AAW Engine Phase Graph)</span>
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            PLAN → EXECUTE → SELF_VERIFY → REVIEW ⮂ REPAIR → FINAL_REVIEW → ROADMAP_CHECK
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* Repair Loop State Badge */}
          {isRepairActive || repairCount > 0 ? (
            <span
              className="text-xs font-mono px-2.5 py-1 rounded-lg bg-phase-repair/15 border border-phase-repair/40 text-rose-300 font-semibold flex items-center gap-1.5 animate-pulse"
              data-testid="repair-loop-badge"
            >
              <RotateCcw className="w-3.5 h-3.5 animate-spin text-rose-400" />
              <span>{`Pętla REPAIR aktywna (Cykl #${repairCount || 1})`}</span>
            </span>
          ) : (
            <span
              className="text-xs font-mono px-2.5 py-1 rounded-lg bg-elevated/40 border border-subtle text-slate-500 flex items-center gap-1.5"
              data-testid="repair-loop-badge"
            >
              <RotateCcw className="w-3.5 h-3.5 text-slate-600" />
              <span>Pętla REPAIR w gotowości</span>
            </span>
          )}

          {/* Human Gate State Badge */}
          {isAwaitingHuman && (
            <span
              className="text-xs font-mono px-2.5 py-1 rounded-lg bg-gate/20 border border-gate/40 text-amber-300 font-bold flex items-center gap-1.5 animate-pulse shadow-md"
              data-testid="human-gate-badge"
            >
              <ShieldAlert className="w-3.5 h-3.5 text-gate" />
              <span>WYMAGA DECYZJI CZŁOWIEKA</span>
            </span>
          )}
        </div>
      </div>

      {/* SVG Canvas for Loopback Curve and Forward Connectors */}
      <div className="w-full relative px-2 mb-2 hidden lg:block" aria-hidden="true">
        <svg
          viewBox="0 0 700 48"
          className="w-full h-12 overflow-visible"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          data-testid="phase-graph-svg"
        >
          <defs>
            {/* Arrow marker for standard forward connection */}
            <marker
              id="arrow-subtle"
              viewBox="0 0 10 10"
              refX="6"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 8 5 L 0 9 z" fill="#334155" />
            </marker>
            {/* Arrow marker for loopback pointing to SELF_VERIFY */}
            <marker
              id="arrow-loopback-active"
              viewBox="0 0 10 10"
              refX="6"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 8 5 L 0 9 z" fill="#f43f5e" />
            </marker>
            <marker
              id="arrow-loopback-idle"
              viewBox="0 0 10 10"
              refX="6"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 8 5 L 0 9 z" fill="#334155" />
            </marker>
          </defs>

          {/* Forward connecting lines between adjacent phase centers (50, 150, 250, 350, 450, 550, 650) */}
          <line x1="88" y1="44" x2="112" y2="44" stroke="#334155" strokeWidth="1.5" markerEnd="url(#arrow-subtle)" />
          <line x1="188" y1="44" x2="212" y2="44" stroke="#334155" strokeWidth="1.5" markerEnd="url(#arrow-subtle)" />
          <line x1="288" y1="44" x2="312" y2="44" stroke="#334155" strokeWidth="1.5" markerEnd="url(#arrow-subtle)" />
          <line
            x1="388"
            y1="44"
            x2="412"
            y2="44"
            stroke={isRepairActive ? "#f43f5e" : "#334155"}
            strokeWidth="1.5"
            markerEnd={isRepairActive ? "url(#arrow-loopback-active)" : "url(#arrow-subtle)"}
          />
          <line x1="488" y1="44" x2="512" y2="44" stroke="#334155" strokeWidth="1.5" markerEnd="url(#arrow-subtle)" />
          <line x1="588" y1="44" x2="612" y2="44" stroke="#334155" strokeWidth="1.5" markerEnd="url(#arrow-subtle)" />

          {/* Subtle direct bypass path (REVIEW -> FINAL_REVIEW when 0 defects) */}
          <path
            d="M 365 42 C 400 30, 500 30, 535 42"
            stroke="#1e293b"
            strokeWidth="1"
            strokeDasharray="2 3"
          />

          {/* Distinctive SVG Loopback Curve: REPAIR (Col 5, x=450) -> SELF_VERIFY (Col 3, x=250) */}
          <path
            data-testid="repair-loopback-curve"
            d="M 450 38 C 450 6, 250 6, 250 38"
            stroke={isRepairActive || repairCount > 0 ? "#f43f5e" : "#202736"}
            strokeWidth={isRepairActive ? "2.5" : "1.5"}
            strokeDasharray={isRepairActive ? "6 4" : "4 4"}
            className={isRepairActive ? "animate-loopback" : ""}
            markerEnd={
              isRepairActive || repairCount > 0
                ? "url(#arrow-loopback-active)"
                : "url(#arrow-loopback-idle)"
            }
          />

          {/* Loopback badge label above the arch */}
          <g transform="translate(350, 11)">
            {isRepairActive || repairCount > 0 ? (
              <g>
                <rect x="-95" y="-9" width="190" height="17" rx="4" fill="#13171f" stroke="#f43f5e" strokeWidth="1" />
                <text
                  x="0"
                  y="3"
                  textAnchor="middle"
                  fill="#f43f5e"
                  fontSize="9"
                  fontFamily="monospace"
                  fontWeight="bold"
                >
                  {`⮂ PĘTLA REPAIR ${repairCount > 0 ? `(Cykl ${repairCount})` : "AKTYWNA"}`}
                </text>
              </g>
            ) : (
              <text
                x="0"
                y="3"
                textAnchor="middle"
                fill="#475569"
                fontSize="8.5"
                fontFamily="monospace"
              >
                ⮂ Pętla naprawcza (REVIEW ⮂ REPAIR → SELF_VERIFY)
              </text>
            )}
          </g>
        </svg>
      </div>

      {/* 7 Phase Nodes Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2.5 relative">
        {phases.map((phase) => (
          <div key={phase.key} className="relative flex-1">
            <PhaseNode
              phase={phase}
              onClick={() => onPhaseSelect?.(phase.key)}
            />
          </div>
        ))}
      </div>

      {/* Underneath Telemetry & Last Event Bar */}
      <div
        className="bg-elevated/40 border border-subtle rounded-xl p-3.5 mt-4 space-y-2.5"
        data-testid="telemetry-bar"
      >
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-xs font-mono min-w-0">
            <span
              className={`w-2 h-2 rounded-full shrink-0 ${
                isAwaitingHuman
                  ? "bg-gate animate-ping"
                  : isRepairActive
                  ? "bg-phase-repair animate-ping"
                  : run?.status === "RUNNING"
                  ? "bg-emerald-400 animate-pulse"
                  : "bg-slate-500"
              }`}
            />
            <span className="text-slate-400 font-bold uppercase shrink-0">Ostatnie działanie:</span>
            <span className="text-slate-200 font-sans text-xs truncate" data-testid="last-activity-text">
              {lastActivityText}
            </span>
          </div>

          <div className="flex items-center gap-2 self-start sm:self-center shrink-0 text-[10px] font-mono text-slate-400">
            <span className="px-2 py-0.5 rounded bg-surface border border-subtle">
              {iterationText}
            </span>
            {isAwaitingHuman && (
              <span className="px-2 py-0.5 rounded bg-gate/20 border border-gate/40 text-amber-300 font-bold animate-pulse">
                BRAMKA CZŁOWIEKA
              </span>
            )}
          </div>
        </div>

        {/* Secondary Telemetry Strip */}
        <div className="pt-2 border-t border-subtle/40 flex flex-wrap items-center justify-between gap-3 text-[11px] font-mono text-slate-400">
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5">
              <User className="w-3 h-3 text-slate-500" />
              <span className="text-slate-500">Rola:</span>
              <span className="text-indigo-300 font-semibold">{activeRole}</span>
            </div>
            <span className="text-slate-700">•</span>
            <div className="flex items-center gap-1.5">
              <Cpu className="w-3 h-3 text-slate-500" />
              <span className="text-slate-500">Model:</span>
              <span className="text-sky-300 font-semibold">{activeModel}</span>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1.5">
              <Activity className="w-3 h-3 text-slate-500" />
              <span className="text-slate-500">Czas:</span>
              <span className="text-slate-300">{elapsedText}</span>
            </div>
            {repairCount > 0 && (
              <>
                <span className="text-slate-700">•</span>
                <div className="flex items-center gap-1.5 text-rose-300 font-semibold">
                  <RotateCcw className="w-3 h-3 text-rose-400" />
                  <span>{`Naprawy: ${repairCount}`}</span>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default PhaseGraph;
