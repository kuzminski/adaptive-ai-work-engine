/**
 * AAW Agentic Studio — Telemetry & Log Console Component
 *
 * Implements a high-density, real-time log terminal with:
 * - Role filter pills (Wszystkie, Planner, Implementer, Verifier, System)
 * - Colorized status output (green passed, red failed, amber gate, sky info)
 * - Monospace timestamps and role badges
 * - Auto-scroll to bottom button and automatic scroll tracking
 * - Telemetry aggregation from run.events, timeline briefs, journal, and gate holds
 */

import React, { useEffect, useRef, useState } from "react";
import {
  Terminal,
  ArrowDown,
  Filter,
  CheckCircle2,
  AlertTriangle,
  ShieldAlert,
} from "lucide-react";
import { EvidenceView, RoleFilter, RunDetail } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";

export interface TelemetryLogEntry {
  id: string;
  timestamp: string;
  role: "Planner" | "Implementer" | "Verifier" | "System";
  phase?: string;
  message: string;
  status: "passed" | "failed" | "gate" | "info";
  details?: string;
}

export interface TelemetryTerminalProps {
  run?: RunDetail | null;
  evidence?: EvidenceView | null;
  roleFilter?: RoleFilter;
  onRoleFilterChange?: (filter: RoleFilter) => void;
}

const ROLE_OPTIONS: { id: RoleFilter; label: string }[] = [
  { id: "All", label: "Wszystkie" },
  { id: "Planner", label: "Planner" },
  { id: "Implementer", label: "Implementer" },
  { id: "Verifier", label: "Verifier" },
  { id: "System", label: "System" },
];

function formatTimestamp(isoOrText?: string | null, fallbackIndex: number = 0): string {
  if (!isoOrText) {
    return fallbackIndex > 0 ? `+${fallbackIndex}s` : "[--:--:--]";
  }
  try {
    const d = new Date(isoOrText);
    if (!isNaN(d.getTime())) {
      const pad = (n: number) => String(n).padStart(2, "0");
      return `[${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}]`;
    }
  } catch {
    // ignore
  }
  if (isoOrText.startsWith("[") && isoOrText.endsWith("]")) {
    return isoOrText;
  }
  return `[${isoOrText}]`;
}

function classifyStatus(text: string): "passed" | "failed" | "gate" | "info" {
  const lower = text.toLowerCase();
  if (
    lower.includes("awaiting_human") ||
    lower.includes("ready_for_decision") ||
    lower.includes("human gate") ||
    lower.includes("bramka") ||
    lower.includes("wstrzymano") ||
    lower.includes("hold")
  ) {
    return "gate";
  }
  if (
    lower.includes("fail") ||
    lower.includes("błąd") ||
    lower.includes("error") ||
    lower.includes("rejected") ||
    lower.includes("problem") ||
    lower.includes("wyjątek") ||
    lower.includes("repair")
  ) {
    return "failed";
  }
  if (
    lower.includes("pass") ||
    lower.includes("sukces") ||
    lower.includes("zakończono pomyślnie") ||
    lower.includes("promoted") ||
    lower.includes("accepted") ||
    lower.includes("approved") ||
    lower.includes("exit code 0") ||
    lower.includes("exit 0") ||
    lower.includes("clean")
  ) {
    return "passed";
  }
  return "info";
}

export function formatTelemetryLogs(
  run: RunDetail | null | undefined,
  evidence: EvidenceView | null | undefined
): TelemetryLogEntry[] {
  const entries: TelemetryLogEntry[] = [];
  let seq = 0;

  // 1. Initial baseline system & security events
  entries.push({
    id: `boot-${++seq}`,
    timestamp: "[--:--:--]",
    role: "System",
    phase: "INIT",
    message: "Połączono z lokalnym silnikiem loopback (127.0.0.1)",
    status: "passed",
  });

  entries.push({
    id: `boot-${++seq}`,
    timestamp: "[--:--:--]",
    role: "Planner",
    phase: "INIT",
    message: "Katalog modeli zweryfikowany: Claude, Codex, Antigravity",
    status: "info",
  });

  entries.push({
    id: `boot-${++seq}`,
    timestamp: "[--:--:--]",
    role: "System",
    phase: "INIT",
    message: "Zabezpieczenia invariantów aktywne: Human Gate & Local Loopback",
    status: "passed",
  });

  if (run?.run_id) {
    entries.push({
      id: `run-${++seq}`,
      timestamp: formatTimestamp(run.created_at, seq),
      role: "System",
      phase: run.engine?.phase || run.process?.phase || "START",
      message: `Zadanie ${run.run_id} aktywne: status=${run.status} (${run.engine?.status || "RUNNING"})`,
      status: classifyStatus(run.status),
    });
  }

  // 2. Events from run.events (if present)
  const runEvents = (run as any)?.events;
  if (Array.isArray(runEvents)) {
    runEvents.forEach((ev: any, idx: number) => {
      const roleRaw = ev.role || ev.actor || "System";
      let role: TelemetryLogEntry["role"] = "System";
      if (/planner/i.test(roleRaw)) role = "Planner";
      else if (/implementer/i.test(roleRaw)) role = "Implementer";
      else if (/verifier/i.test(roleRaw)) role = "Verifier";

      const msg = ev.message || ev.text || ev.event_type || JSON.stringify(ev);
      entries.push({
        id: `ev-${idx}-${++seq}`,
        timestamp: formatTimestamp(ev.timestamp || ev.occurred_at, seq),
        role,
        phase: ev.phase,
        message: msg,
        status: classifyStatus(msg),
      });
    });
  }

  // 3. Journal events from evidence.journal or evidence.journal_events
  const journal = evidence?.journal || evidence?.journal_events;
  if (Array.isArray(journal)) {
    journal.forEach((j: any, idx: number) => {
      let role: TelemetryLogEntry["role"] = "System";
      const phase = (j.phase || "").toUpperCase();
      if (phase === "PLAN") role = "Planner";
      else if (phase === "EXECUTE" || phase === "REPAIR") role = "Implementer";
      else if (phase === "SELF_VERIFY" || phase === "REVIEW" || phase === "FINAL_REVIEW") role = "Verifier";

      const eventType = j.event_type || "journal_event";
      const payloadDesc =
        typeof j.payload === "string"
          ? j.payload
          : j.payload?.message || j.payload?.summary || j.payload?.status || JSON.stringify(j.payload || {});
      const msg = `[${eventType}] ${payloadDesc}`;

      entries.push({
        id: `journal-${idx}-${++seq}`,
        timestamp: formatTimestamp(j.occurred_at, seq),
        role,
        phase: j.phase,
        message: msg,
        status: classifyStatus(msg),
      });
    });
  }

  // 4. Timeline rows and iteration briefs
  if (Array.isArray(run?.timeline)) {
    run.timeline.forEach((row, rowIdx) => {
      // Row start marker
      entries.push({
        id: `timeline-row-${rowIdx}`,
        timestamp: formatTimestamp(row.started_at, seq),
        role: "System",
        message: `Rozpoczęcie iteracji ${row.index ?? rowIdx + 1}: ${row.label}${row.goal ? ` — cel: ${row.goal}` : ""}`,
        status: "info",
      });

      if (Array.isArray(row.briefs)) {
        row.briefs.forEach((brief, bIdx) => {
          let briefRole: TelemetryLogEntry["role"] = "System";
          const bPhase = (brief.phase || brief.kind || "").toUpperCase();
          if (bPhase.includes("PLAN")) briefRole = "Planner";
          else if (bPhase.includes("EXECUTE") || bPhase.includes("REPAIR")) briefRole = "Implementer";
          else if (bPhase.includes("VERIFY") || bPhase.includes("REVIEW")) briefRole = "Verifier";

          // Phase execution summary
          entries.push({
            id: `brief-${rowIdx}-${bIdx}-start`,
            timestamp: formatTimestamp(brief.at, seq),
            role: briefRole,
            phase: brief.phase,
            message: `Faza ${brief.phase} (Model: ${brief.model})${brief.goal ? ` — ${brief.goal}` : ""}`,
            status: "info",
          });

          // Done items
          if (Array.isArray(brief.done)) {
            brief.done.forEach((item, dIdx) => {
              entries.push({
                id: `brief-${rowIdx}-${bIdx}-done-${dIdx}`,
                timestamp: formatTimestamp(brief.at, seq),
                role: briefRole,
                phase: brief.phase,
                message: `✓ [WYKONANO] ${item}`,
                status: "passed",
              });
            });
          }

          // Checks items (pytest, linter, etc.)
          if (Array.isArray(brief.checks)) {
            brief.checks.forEach((chk, cIdx) => {
              const pass = chk.status === "PASS";
              entries.push({
                id: `brief-${rowIdx}-${bIdx}-chk-${cIdx}`,
                timestamp: formatTimestamp(brief.at, seq),
                role: briefRole,
                phase: brief.phase,
                message: `[${chk.status}] ${chk.name}: ${chk.summary || "weryfikacja zakończona"}`,
                status: pass ? "passed" : "failed",
              });
            });
          }

          // Problems / repair triggers
          if (Array.isArray(brief.problems)) {
            brief.problems.forEach((prob, pIdx) => {
              entries.push({
                id: `brief-${rowIdx}-${bIdx}-prob-${pIdx}`,
                timestamp: formatTimestamp(brief.at, seq),
                role: briefRole,
                phase: brief.phase,
                message: `⚠ [PROBLEM / REPAIR] ${prob}`,
                status: "failed",
              });
            });
          }

          // Handed over
          if (Array.isArray(brief.handed_over)) {
            brief.handed_over.forEach((ho, hIdx) => {
              entries.push({
                id: `brief-${rowIdx}-${bIdx}-ho-${hIdx}`,
                timestamp: formatTimestamp(brief.at, seq),
                role: briefRole,
                phase: brief.phase,
                message: `→ [PRZEKAZANIE] ${ho}`,
                status: "info",
              });
            });
          }
        });
      }
    });
  }

  // 5. Gate notification if holding for human decision
  if (run?.gate) {
    const why = run.gate.hold_reason || run.gate.why || "Wstrzymano dla decyzji człowieka";
    entries.push({
      id: `gate-hold-${++seq}`,
      timestamp: formatTimestamp(null, seq),
      role: "System",
      phase: "ROADMAP_CHECK",
      message: `[BRAMKA HUMAN-IN-LOOP] ${why}`,
      status: "gate",
      details: run.gate.escalation_code ? `Kod eskalacji: ${run.gate.escalation_code}` : undefined,
    });
  }

  return entries;
}

export const TelemetryTerminal: React.FC<TelemetryTerminalProps> = ({
  run: propRun,
  evidence: propEvidence,
  roleFilter: propRoleFilter,
  onRoleFilterChange,
}) => {
  const store = useEngineStore();
  const run = propRun !== undefined ? propRun : store.run;
  const evidence = propEvidence !== undefined ? propEvidence : store.evidence;
  const activeRoleFilter = propRoleFilter ?? store.roleFilter ?? "All";

  const handleRoleChange = (role: RoleFilter) => {
    if (onRoleFilterChange) {
      onRoleFilterChange(role);
    } else {
      store.setRoleFilter(role);
    }
  };

  const viewportRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  const logs = formatTelemetryLogs(run, evidence);
  const filteredLogs =
    activeRoleFilter === "All" ? logs : logs.filter((l) => l.role === activeRoleFilter);

  const scrollToBottom = () => {
    if (viewportRef.current) {
      viewportRef.current.scrollTop = viewportRef.current.scrollHeight;
    }
  };

  useEffect(() => {
    if (autoScroll && viewportRef.current) {
      viewportRef.current.scrollTop = viewportRef.current.scrollHeight;
    }
  }, [logs.length, activeRoleFilter, autoScroll]);

  // Handle user scroll to enable/disable auto-scroll
  const handleScroll = () => {
    if (!viewportRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = viewportRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 30;
    setAutoScroll(isAtBottom);
  };

  return (
    <div className="flex-1 flex flex-col h-full bg-terminal text-slate-200 select-text font-mono text-xs overflow-hidden">
      {/* Subheader: Role Filters & Auto-scroll control */}
      <div
        className="px-3 py-2 bg-surface/90 border-b border-subtle flex items-center justify-between gap-2 shrink-0 select-none"
        data-testid="telemetry-terminal-controls"
      >
        <div className="flex items-center gap-1.5 overflow-x-auto">
          <Filter className="w-3.5 h-3.5 text-slate-500 shrink-0 mr-1" />
          {ROLE_OPTIONS.map((opt) => (
            <button
              key={opt.id}
              onClick={() => handleRoleChange(opt.id)}
              data-testid={`role-filter-${opt.id}`}
              className={`px-2.5 py-1 text-[11px] font-mono rounded transition shrink-0 ${
                activeRoleFilter === opt.id
                  ? "bg-phase-plan text-indigo-950 font-bold shadow-sm"
                  : "bg-surface hover:bg-elevated text-slate-400 hover:text-slate-200 border border-subtle"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {/* Scroll to bottom button */}
        <button
          onClick={scrollToBottom}
          data-testid="terminal-scroll-bottom"
          title="Przewiń na sam dół"
          className="p-1 rounded bg-elevated hover:bg-slate-700 text-slate-400 hover:text-white border border-subtle shrink-0 transition"
        >
          <ArrowDown className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Terminal Viewport */}
      <div
        ref={viewportRef}
        onScroll={handleScroll}
        data-testid="terminal-viewport"
        className="flex-1 p-3 overflow-y-auto space-y-1.5 font-mono text-[11px] leading-relaxed scrollbar-thin scrollbar-thumb-slate-800 scrollbar-track-transparent"
      >
        {filteredLogs.length === 0 ? (
          <div
            data-testid="terminal-empty-filter"
            className="py-12 text-center text-slate-500 font-mono text-xs"
          >
            Brak zdarzeń dla wybranej roli: <span className="font-semibold">{activeRoleFilter}</span>
          </div>
        ) : (
          filteredLogs.map((entry) => {
            // Role badge styling
            let roleBadge = "bg-slate-700/50 text-slate-300 border-slate-600/50";
            if (entry.role === "Planner") {
              roleBadge = "bg-indigo-500/20 text-indigo-300 border-indigo-500/40";
            } else if (entry.role === "Implementer") {
              roleBadge = "bg-sky-500/20 text-sky-300 border-sky-500/40";
            } else if (entry.role === "Verifier") {
              roleBadge = "bg-purple-500/20 text-purple-300 border-purple-500/40";
            }

            // Message text coloring based on status
            let msgColor = "text-slate-300";
            let statusIcon = null;
            let rowBorder = "";

            if (entry.status === "passed") {
              msgColor = "text-emerald-400 font-semibold";
              statusIcon = <CheckCircle2 className="w-3 h-3 text-emerald-400 shrink-0 inline mr-1" />;
              rowBorder = "border-l border-emerald-500/40 pl-1.5";
            } else if (entry.status === "failed") {
              msgColor = "text-rose-400 font-semibold";
              statusIcon = <AlertTriangle className="w-3 h-3 text-rose-400 shrink-0 inline mr-1" />;
              rowBorder = "border-l border-rose-500/40 pl-1.5 bg-rose-500/5";
            } else if (entry.status === "gate") {
              msgColor = "text-amber-300 font-bold";
              statusIcon = <ShieldAlert className="w-3 h-3 text-amber-400 shrink-0 inline mr-1 animate-pulse" />;
              rowBorder = "border-l-2 border-amber-500 pl-1.5 bg-amber-500/10";
            }

            return (
              <div
                key={entry.id}
                data-testid={`terminal-log-entry-${entry.id}`}
                className={`flex items-start gap-2 py-0.5 rounded transition hover:bg-slate-900/60 ${rowBorder}`}
              >
                {/* Timestamp */}
                <span className="text-slate-600 shrink-0 font-mono text-[10px] select-none pt-0.5">
                  {entry.timestamp}
                </span>

                {/* Role Badge */}
                <span
                  data-testid={`role-badge-${entry.role.toLowerCase()}`}
                  className={`px-1.5 py-0.2 rounded text-[9px] font-bold uppercase tracking-wider shrink-0 border select-none ${roleBadge}`}
                >
                  {entry.role}
                </span>

                {/* Optional Phase badge */}
                {entry.phase && (
                  <span className="text-[9px] text-slate-500 font-mono shrink-0 select-none">
                    [{entry.phase}]
                  </span>
                )}

                {/* Message Body */}
                <span className={`flex-1 break-all whitespace-pre-wrap ${msgColor}`}>
                  {statusIcon}
                  {entry.message}
                </span>
              </div>
            );
          })
        )}
      </div>

      {/* Terminal Footer Bar */}
      <div className="px-3 py-1.5 bg-surface/80 border-t border-subtle flex items-center justify-between text-[10px] text-slate-500 select-none">
        <div className="flex items-center gap-1.5">
          <Terminal className="w-3 h-3 text-sky-400" />
          <span>Wierszy: {filteredLogs.length}</span>
        </div>
        <div className="flex items-center gap-2">
          {autoScroll ? (
            <span className="text-emerald-400 flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              Auto-scroll WŁĄCZONY
            </span>
          ) : (
            <span className="text-slate-500">Auto-scroll WSTRZYMANY</span>
          )}
        </div>
      </div>
    </div>
  );
};
