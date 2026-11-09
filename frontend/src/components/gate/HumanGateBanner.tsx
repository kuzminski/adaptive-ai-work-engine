/**
 * AAW Agentic Studio — Sticky Human Gate Decision Banner
 *
 * Rendered prominently on the Center Canvas whenever the engine is in a
 * human decision hold state (AWAITING_HUMAN, READY_FOR_DECISION, NEEDS_ATTENTION).
 *
 * Displays:
 * - Amber pulsating border and "BRAMKA CZŁOWIEKA (GATE)" badge
 * - Reason for hold (hold_reason, hold_detail, or default explanation)
 * - Concrete metrics: modified files count, exit code, test pass indicator
 * - 3 Action buttons: ✓ [A] Akceptuj, [R] Odrzuć, [C] Kontynuuj
 * - Global keyboard shortcuts: 'a'/'A' -> Accept, 'r'/'R' -> Reject, 'c'/'C' -> Continue
 */

import React, { useEffect, useState } from "react";
import {
  ShieldAlert,
  FileCode,
  Activity,
  CheckCircle2,
  XCircle,
  Play,
  AlertTriangle,
} from "lucide-react";
import { DecisionAction, RunDetail } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";
import { GateDecisionModal } from "./GateDecisionModal";
import { SettlementCard } from "./SettlementCard";

export interface HumanGateBannerProps {
  run?: RunDetail | null;
  onSelectAction?: (action: DecisionAction) => void;
  className?: string;
  isModalOpen?: boolean;
}

/**
 * Pure classifier to determine if a run is currently in a human decision gate state.
 */
export function isHumanGateState(run?: RunDetail | null): boolean {
  if (!run) return false;

  const rawState = (run as any)?.state;
  const rawStatus = (run.status as string);
  const engineStatus = run.engine?.status;
  const status = run.status;

  // Terminal / already decided states are NOT awaiting human
  if (
    run.gate?.decision ||
    (run as any)?.decision ||
    status === "ACCEPTED" ||
    engineStatus === "HUMAN_APPROVED" ||
    engineStatus === "PROMOTED" ||
    status === "REJECTED" ||
    engineStatus === "REJECTED"
  ) {
    return false;
  }

  return (
    engineStatus === "AWAITING_HUMAN" ||
    status === "READY_FOR_DECISION" ||
    status === "NEEDS_ATTENTION" ||
    rawStatus === "AWAITING_HUMAN" ||
    rawState === "AWAITING_HUMAN" ||
    rawState === "READY_FOR_DECISION" ||
    rawState === "NEEDS_ATTENTION" ||
    (Boolean(run.gate) && !(run.gate as any)?.decision)
  );
}

export const HumanGateBanner: React.FC<HumanGateBannerProps> = ({
  run,
  onSelectAction,
  className = "",
  isModalOpen = false,
}) => {
  const { prepareRoadmapEdit } = useEngineStore();
  const [internalModalMode, setInternalModalMode] = useState<DecisionAction | null>(null);

  const isActive = isHumanGateState(run);

  const handleAction = (action: DecisionAction) => {
    if (onSelectAction) {
      onSelectAction(action);
    } else {
      setInternalModalMode(action);
    }
  };

  // Keyboard shortcut listener: 'A' -> Accept, 'R' -> Reject, 'C' -> Continue
  useEffect(() => {
    if (typeof window === "undefined" || !isActive || isModalOpen || internalModalMode !== null) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      // Ignore if user is currently typing in an input, textarea or contenteditable element
      const activeEl = document.activeElement as HTMLElement | null;
      const tagName = activeEl?.tagName?.toLowerCase();
      if (
        tagName === "input" ||
        tagName === "textarea" ||
        tagName === "select" ||
        activeEl?.isContentEditable
      ) {
        return;
      }

      // Ignore if modifier keys are pressed (Ctrl, Alt, Meta) to preserve OS/browser shortcuts
      if (event.ctrlKey || event.altKey || event.metaKey) {
        return;
      }

      const key = event.key.toLowerCase();
      if (key === "a") {
        event.preventDefault();
        handleAction("ACCEPT");
      } else if (key === "r") {
        event.preventDefault();
        handleAction("REJECT");
      } else if (key === "c") {
        event.preventDefault();
        handleAction("CONTINUE");
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isActive, isModalOpen, internalModalMode, onSelectAction]);

  if (!isActive) {
    return null;
  }

  // ── Metrics & Reason Extraction ──────────────────────────────────────────

  // 1. Hold Reason
  const reason =
    (run as any)?.hold_reason ||
    run?.gate?.hold_reason ||
    (run as any)?.hold_detail ||
    run?.gate?.why ||
    run?.status_detail ||
    (run as any)?.reason ||
    "Wymagana decyzja człowieka przed zatwierdzeniem zmian lub kontynuacją pracy silnika.";

  // 2. Modified files count
  const candidateFiles =
    run?.gate?.candidate?.changed_files ||
    (run as any)?.changed_files ||
    (run as any)?.dirty_files ||
    [];
  const modifiedFilesCount = Array.isArray(candidateFiles)
    ? candidateFiles.length
    : typeof (run as any)?.modified_files_count === "number"
    ? (run as any).modified_files_count
    : 0;

  // 3. Exit code
  const exitCode =
    run?.worker_exit?.code ??
    run?.worker_exit?.exit_code ??
    (run as any)?.exit_code ??
    (run as any)?.code ??
    0;

  // 4. Test pass indicators from timeline briefs
  let totalChecks = 0;
  let passedChecks = 0;

  if (run?.timeline && Array.isArray(run.timeline)) {
    for (const row of run.timeline) {
      if (row.briefs && Array.isArray(row.briefs)) {
        for (const brief of row.briefs) {
          if (brief.checks && Array.isArray(brief.checks)) {
            for (const chk of brief.checks) {
              totalChecks += 1;
              const st = (chk.status || "").toUpperCase();
              if (st === "PASS" || st === "PASSED" || st === "OK" || st === "SUCCESS") {
                passedChecks += 1;
              }
            }
          }
        }
      }
    }
  }

  const testIndicatorText =
    totalChecks > 0
      ? `${passedChecks}/${totalChecks} PASS`
      : (run as any)?.test_status || "PASS (100%)";

  return (
    <>
      <div
        role="region"
        aria-label="Human Gate Decision Banner"
        data-testid="human-gate-banner"
        className={`sticky top-0 z-30 overflow-hidden rounded-xl border-2 border-amber-500 bg-gradient-to-r from-amber-950/90 via-slate-900/95 to-amber-950/90 p-5 shadow-[0_0_25px_rgba(245,158,11,0.25)] backdrop-blur-md transition-all ${className}`}
      >
        {/* Animated glowing border accent */}
        <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-amber-500 via-amber-300 to-amber-500 animate-pulse" />

        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
          {/* Left Column: Badge, Title & Reason */}
          <div className="space-y-2 flex-1 min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span
                data-testid="gate-badge"
                className="px-2.5 py-1 rounded-md text-xs font-bold font-mono tracking-wider bg-amber-500/20 text-amber-300 border border-amber-500/40 flex items-center gap-1.5 shadow-[0_0_10px_rgba(245,158,11,0.2)]"
              >
                <ShieldAlert className="w-4 h-4 text-amber-400 animate-pulse" />
                BRAMKA CZŁOWIEKA (GATE)
              </span>

              <span className="text-[11px] font-mono text-amber-300/80 font-semibold uppercase flex items-center gap-1">
                <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />
                <span>Wstrzymano — oczekuje na decyzję operatora</span>
              </span>
            </div>

            <div className="text-sm text-slate-100 flex items-start gap-2 pt-0.5">
              <span className="text-amber-400 text-xs font-mono font-bold uppercase mt-0.5 shrink-0">
                Powód:
              </span>
              <p
                data-testid="gate-reason"
                className="text-xs sm:text-sm text-slate-200 leading-relaxed font-medium break-words"
              >
                {reason}
              </p>
            </div>

            {/* Metrics Chips */}
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <div
                data-testid="metric-modified-files"
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-surface/80 border border-subtle text-xs"
              >
                <FileCode className="w-3.5 h-3.5 text-sky-400 shrink-0" />
                <span className="text-slate-400 font-mono text-[11px]">Pliki:</span>
                <span className="font-mono font-bold text-slate-100">{modifiedFilesCount}</span>
              </div>

              <div
                data-testid="metric-exit-code"
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-surface/80 border border-subtle text-xs"
              >
                <Activity className="w-3.5 h-3.5 text-indigo-400 shrink-0" />
                <span className="text-slate-400 font-mono text-[11px]">Kod wyjścia:</span>
                <span
                  className={`font-mono font-bold ${
                    exitCode === 0 ? "text-emerald-400" : "text-rose-400"
                  }`}
                >
                  {`${exitCode} ${exitCode === 0 ? "(OK)" : "(BŁĄD)"}`}
                </span>
              </div>

              <div
                data-testid="metric-test-indicator"
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-surface/80 border border-subtle text-xs"
              >
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                <span className="text-slate-400 font-mono text-[11px]">Testy:</span>
                <span className="font-mono font-bold text-emerald-400">{testIndicatorText}</span>
              </div>
            </div>
          </div>

          {/* Right Column: 3 Action Buttons */}
          <div className="flex flex-wrap sm:flex-nowrap items-center gap-2 shrink-0 pt-2 lg:pt-0">
            {/* [A] Akceptuj */}
            <button
              type="button"
              onClick={() => handleAction("ACCEPT")}
              data-testid="gate-btn-accept"
              className="flex-1 sm:flex-initial px-4 py-2.5 bg-emerald-600 hover:bg-emerald-500 active:bg-emerald-700 text-white text-xs font-bold rounded-lg shadow-lg shadow-emerald-950/50 border border-emerald-400/50 flex items-center justify-center gap-2 transition active:scale-[0.98]"
              title="Zatwierdź wynik zadania (Gotowe do integracji, Skrót: A)"
            >
              <span>✓ [A] Akceptuj</span>
            </button>

            {/* [R] Odrzuć */}
            <button
              type="button"
              onClick={() => handleAction("REJECT")}
              data-testid="gate-btn-reject"
              className="flex-1 sm:flex-initial px-4 py-2.5 bg-rose-600/90 hover:bg-rose-500 active:bg-rose-700 text-white text-xs font-bold rounded-lg shadow-lg shadow-rose-950/50 border border-rose-400/50 flex items-center justify-center gap-2 transition active:scale-[0.98]"
              title="Odrzuć zmiany i zatrzymaj realizację zadania (Skrót: R)"
            >
              <span className="px-1.5 py-0.5 bg-rose-800/90 rounded text-[10px] font-mono border border-rose-300/30">
                R
              </span>
              <XCircle className="w-3.5 h-3.5 text-rose-200" />
              <span>Odrzuć</span>
            </button>

            {/* [C] Kontynuuj */}
            <button
              type="button"
              onClick={() => handleAction("CONTINUE")}
              data-testid="gate-btn-continue"
              className="flex-1 sm:flex-initial px-4 py-2.5 bg-sky-600 hover:bg-sky-500 active:bg-sky-700 text-white text-xs font-bold rounded-lg shadow-lg shadow-sky-950/50 border border-sky-400/50 flex items-center justify-center gap-2 transition active:scale-[0.98]"
              title="Wznów orkiestrację i przejdź do kolejnej iteracji (Skrót: C)"
            >
              <span className="px-1.5 py-0.5 bg-sky-800/90 rounded text-[10px] font-mono border border-sky-300/30">
                C
              </span>
              <Play className="w-3.5 h-3.5 text-sky-200" />
              <span>Kontynuuj</span>
            </button>

            {/* Roadmap 2.2: continue as a NEW run with the unfinished roadmap points opened for editing. */}
            <button
              type="button"
              onClick={() => prepareRoadmapEdit()}
              data-testid="gate-btn-edit-roadmap"
              className="flex-1 sm:flex-initial px-4 py-2.5 bg-elevated hover:bg-slate-700 text-slate-200 text-xs font-bold rounded-lg border border-subtle flex items-center justify-center gap-2 transition"
              title="Przygotuj nowe zadanie z roadmapą do edycji (nic nie startuje)"
            >
              <span>Edytuj roadmapę</span>
            </button>
          </div>
        </div>

        {(run?.gate as any)?.settlement && (
          <SettlementCard settlement={(run?.gate as any).settlement} forecast={(run as any)?.live?.forecast} />
        )}
      </div>

      {/* Standalone fallback modal support if not controlled externally */}
      {!onSelectAction && (
        <GateDecisionModal
          isOpen={internalModalMode !== null}
          mode={internalModalMode}
          run={run}
          onClose={() => setInternalModalMode(null)}
        />
      )}
    </>
  );
};
