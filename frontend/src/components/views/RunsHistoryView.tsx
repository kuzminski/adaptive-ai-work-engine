/**
 * AAW Agentic Studio — Runs History View
 *
 * Comprehensive run management and historical ledger view:
 * - Groups and filters runs from `/api/runs` (running, attention, paused, completed)
 * - Search by Run ID, project name, or goal keywords
 * - Real execution actions: Load into Command Center, Resume paused runs, Stop running tasks
 */

import React, { useState, useEffect, useMemo } from "react";
import {
  History,
  Search,
  RefreshCw,
  Play,
  Square,
  ShieldAlert,
  CheckCircle2,
  Clock,
  Repeat,
  ArrowRight,
  AlertTriangle,
} from "lucide-react";
import { client } from "../../api/client";
import { RunSummaryCard, RunStatusCategory } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";

export const RunsHistoryView: React.FC = () => {
  const {
    runs,
    loading,
    activeRunId,
    loadRuns,
    selectRun,
    setActiveNav,
  } = useEngineStore();

  const [searchQuery, setSearchQuery] = useState<string>("");
  const [selectedSection, setSelectedSection] = useState<string>("all");
  const [actionInProgress, setActionInProgress] = useState<string | null>(null);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  // Aggregate all runs across sections
  const allRuns = useMemo(() => {
    if (!runs?.sections) return [];
    const running = (runs.sections.running || []).map((r) => ({ ...r, category: "running" }));
    const attention = (runs.sections.attention || []).map((r) => ({ ...r, category: "attention" }));
    const paused = (runs.sections.paused || []).map((r) => ({ ...r, category: "paused" }));
    const completed = (runs.sections.completed || []).map((r) => ({ ...r, category: "completed" }));
    return [...running, ...attention, ...paused, ...completed];
  }, [runs]);

  // Filtered runs
  const filteredRuns = useMemo(() => {
    return allRuns.filter((item) => {
      // Section filter
      if (selectedSection !== "all" && item.category !== selectedSection) {
        return false;
      }
      // Query filter
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchId = (item.run_id || "").toLowerCase().includes(q);
        const matchProject = (item.project || "").toLowerCase().includes(q);
        const matchGoal = (item.goal || item.short_goal || "").toLowerCase().includes(q);
        const matchStatus = (item.status_label || item.status || "").toLowerCase().includes(q);
        return matchId || matchProject || matchGoal || matchStatus;
      }
      return true;
    });
  }, [allRuns, selectedSection, searchQuery]);

  const handleOpenRun = async (runId: string) => {
    await selectRun(runId);
    setActiveNav("command-center");
  };

  const handleResumeRun = async (run: RunSummaryCard) => {
    setActionInProgress(`resume-${run.run_id}`);
    try {
      await client.postResume(run.run_id, run.lock_token || null);
      await loadRuns();
    } finally {
      setActionInProgress(null);
    }
  };

  const handleStopRun = async (run: RunSummaryCard) => {
    setActionInProgress(`stop-${run.run_id}`);
    try {
      await client.postStop(run.run_id, false);
      await loadRuns();
    } finally {
      setActionInProgress(null);
    }
  };

  const getStatusBadge = (status: RunStatusCategory | string, label?: string) => {
    const s = String(status).toUpperCase();
    if (s === "RUNNING" || s === "STARTING") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-sky-500/10 text-sky-400 border border-sky-500/30 flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-sky-400 animate-pulse" />
          <span>{label || "Pracuje"}</span>
        </span>
      );
    }
    if (s === "READY_FOR_DECISION" || s === "NEEDS_ATTENTION" || s === "AWAITING_HUMAN") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/30 flex items-center gap-1">
          <ShieldAlert className="w-3 h-3 text-amber-400" />
          <span>{label || "Czeka na decyzję"}</span>
        </span>
      );
    }
    if (s === "ACCEPTED" || s === "PROMOTED" || s === "HUMAN_APPROVED") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center gap-1">
          <CheckCircle2 className="w-3 h-3 text-emerald-400" />
          <span>{label || "Zaakceptowane"}</span>
        </span>
      );
    }
    if (s === "PAUSED" || s === "INTERRUPTED") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-slate-700/50 text-slate-300 border border-slate-600 flex items-center gap-1">
          <Clock className="w-3 h-3 text-slate-400" />
          <span>{label || "Wstrzymane"}</span>
        </span>
      );
    }
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/30 flex items-center gap-1">
        <AlertTriangle className="w-3 h-3 text-rose-400" />
        <span>{label || s}</span>
      </span>
    );
  };

  return (
    <div className="max-w-5xl w-full mx-auto space-y-6 animate-fade-in pb-12" data-testid="runs-history-view">
      {/* Header Card */}
      <div className="bg-surface border border-subtle rounded-xl p-5 shadow-xl flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-xs font-mono text-sky-400 uppercase mb-1">
            <History className="w-3.5 h-3.5" />
            <span>Księga Zadań • Dziennik Wykonań</span>
          </div>
          <h2 className="text-xl font-bold tracking-tight text-white">
            Historia i Status Zadań (Task Runs)
          </h2>
          <p className="text-xs text-slate-400 mt-1 max-w-2xl">
            Przeglądaj wszystkie uruchomienia silnika AAW. Możesz załadować dowolne zadanie do Live Command Center, wznowić przerwane przebiegi lub dokonać inspekcji dowodów.
          </p>
        </div>

        <div className="flex items-center gap-2 self-start sm:self-center">
          <button
            onClick={() => loadRuns()}
            disabled={loading}
            className="px-3 py-1.5 bg-elevated hover:bg-slate-700 text-xs font-medium text-slate-200 border border-subtle rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin text-sky-400" : ""}`} />
            <span>Odśwież listę</span>
          </button>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="flex flex-col sm:flex-row items-center justify-between gap-3">
        {/* Category Tabs */}
        <div className="flex items-center gap-1 bg-surface border border-subtle p-1 rounded-xl w-full sm:w-auto overflow-x-auto">
          {[
            { id: "all", label: "Wszystkie", count: allRuns.length },
            { id: "running", label: "W toku", count: runs?.sections?.running?.length || 0 },
            { id: "attention", label: "Wymaga decyzji", count: runs?.sections?.attention?.length || 0 },
            { id: "paused", label: "Wstrzymane", count: runs?.sections?.paused?.length || 0 },
            { id: "completed", label: "Zakończone", count: runs?.sections?.completed?.length || 0 },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setSelectedSection(tab.id)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition flex items-center gap-1.5 shrink-0 ${
                selectedSection === tab.id
                  ? "bg-sky-600 text-white font-semibold shadow"
                  : "text-slate-400 hover:text-slate-200 hover:bg-elevated/40"
              }`}
            >
              <span>{tab.label}</span>
              <span className={`text-[10px] px-1.5 py-0.2 rounded-full font-mono ${
                selectedSection === tab.id ? "bg-sky-700 text-white" : "bg-elevated text-slate-400"
              }`}>
                {tab.count}
              </span>
            </button>
          ))}
        </div>

        {/* Search Input */}
        <div className="relative w-full sm:w-72">
          <Search className="w-3.5 h-3.5 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Szukaj po ID, projekcie, celu..."
            className="w-full bg-surface border border-subtle rounded-xl pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-sky-500 font-mono"
          />
        </div>
      </div>

      {/* Runs List */}
      {filteredRuns.length === 0 ? (
        <div className="bg-surface border border-subtle rounded-xl p-12 text-center space-y-3">
          <History className="w-8 h-8 text-slate-600 mx-auto" />
          <div className="text-sm font-semibold text-slate-300">Brak zadań w wybranej kategorii</div>
          <p className="text-xs text-slate-500 max-w-md mx-auto">
            {searchQuery
              ? "Żadne uruchomienie nie pasuje do podanego zapytania."
              : "Nie uruchomiono jeszcze żadnego zadania lub lista jest pusta."}
          </p>
          <button
            onClick={() => setActiveNav("new-task")}
            className="mt-2 px-4 py-2 bg-sky-600 hover:bg-sky-500 text-xs font-medium text-white rounded-lg inline-flex items-center gap-2 transition"
          >
            <span>Utwórz pierwsze zadanie</span>
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3">
          {filteredRuns.map((r) => {
            const isSelected = activeRunId === r.run_id;
            const isRunInProgress = r.status === "RUNNING" || r.status === "STARTING";

            return (
              <div
                key={r.run_id}
                className={`bg-surface border rounded-xl p-4 transition shadow-lg space-y-3 ${
                  isSelected
                    ? "border-sky-500/60 ring-1 ring-sky-500/40 bg-surface/90"
                    : "border-subtle hover:border-slate-600"
                }`}
              >
                {/* Header row */}
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                  <div className="flex items-center gap-2.5 flex-wrap">
                    <span className="font-mono text-xs font-bold text-white bg-elevated px-2 py-0.5 rounded border border-subtle">
                      {r.run_id}
                    </span>
                    {getStatusBadge(r.status, r.status_label)}
                    {isSelected && (
                      <span className="text-[10px] uppercase font-bold text-sky-400 bg-sky-500/10 px-2 py-0.5 rounded border border-sky-500/30">
                        Wybrany w Studio
                      </span>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-2 shrink-0">
                    {r.can_resume && (
                      <button
                        onClick={() => handleResumeRun(r)}
                        disabled={actionInProgress !== null}
                        className="px-2.5 py-1 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 border border-emerald-500/40 text-xs font-medium rounded-lg flex items-center gap-1 transition disabled:opacity-50"
                        title="Wznów zadanie"
                      >
                        <Play className="w-3 h-3 text-emerald-400" />
                        <span>Wznów</span>
                      </button>
                    )}

                    {isRunInProgress && (
                      <button
                        onClick={() => handleStopRun(r)}
                        disabled={actionInProgress !== null}
                        className="px-2.5 py-1 bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 text-xs font-medium rounded-lg flex items-center gap-1 transition disabled:opacity-50"
                        title="Zatrzymaj bezpiecznie"
                      >
                        <Square className="w-3 h-3 text-amber-400" />
                        <span>Zatrzymaj</span>
                      </button>
                    )}

                    <button
                      onClick={() => handleOpenRun(r.run_id)}
                      className="px-3 py-1 bg-sky-600/20 hover:bg-sky-600/30 text-sky-300 border border-sky-500/40 text-xs font-medium rounded-lg flex items-center gap-1.5 transition"
                    >
                      <span>Załaduj do Studio</span>
                      <ArrowRight className="w-3 h-3" />
                    </button>
                  </div>
                </div>

                {/* Project & Goal info */}
                <div>
                  <div className="text-sm font-semibold text-slate-100">
                    {r.project || "Projekt bez nazwy"}
                  </div>
                  <p className="text-xs text-slate-400 mt-0.5 line-clamp-2 leading-relaxed">
                    {r.goal || r.short_goal || "Brak opisu celu"}
                  </p>
                </div>

                {/* Meta details footer */}
                <div className="flex items-center gap-4 text-[11px] font-mono text-slate-500 border-t border-subtle/60 pt-2 flex-wrap">
                  {r.iteration !== undefined && (
                    <div className="flex items-center gap-1">
                      <Repeat className="w-3 h-3 text-slate-400" />
                      <span>Iteracja: {r.iteration}</span>
                    </div>
                  )}
                  {r.activity && (
                    <div className="flex items-center gap-1 text-slate-400">
                      <span>Aktywność: {r.activity}</span>
                    </div>
                  )}
                  {r.roadmap && (
                    <div className="flex items-center gap-1">
                      <span>Roadmapa: {r.roadmap.done} / {r.roadmap.total} ukończone</span>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
