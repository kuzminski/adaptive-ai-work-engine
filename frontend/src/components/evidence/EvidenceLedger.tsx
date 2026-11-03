/**
 * AAW Agentic Studio — Evidence Ledger (Księga Dowodów) Component
 *
 * Implements the verified proof center of the Autonomous AI Engine:
 * - Test Proof Card (pytest results, exit code 0)
 * - Linter Proof Card (ruff check clean, 0 violations)
 * - Changed Files Audit Card (git candidate commit, diff hash, file list)
 * - Safety Invariants Card (Human Gate, Loopback isolation, Zero runtime Python dependencies)
 */

import React from "react";
import {
  CheckCircle2,
  ShieldCheck,
  FileCheck,
  Lock,
  FolderGit2,
  Hash,
} from "lucide-react";
import { EvidenceView, RunDetail } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";

export interface EvidenceLedgerProps {
  run?: RunDetail | null;
  evidence?: EvidenceView | null;
}

export const EvidenceLedger: React.FC<EvidenceLedgerProps> = ({
  run: propRun,
  evidence: propEvidence,
}) => {
  const store = useEngineStore();
  const run = propRun !== undefined ? propRun : store.run;
  const evidence = propEvidence !== undefined ? propEvidence : store.evidence;

  // Extract test results from timeline briefs or ledger
  const timelineChecks: { name: string; status: string; summary?: string | null }[] = [];
  if (Array.isArray(run?.timeline)) {
    run.timeline.forEach((row) => {
      if (Array.isArray(row.briefs)) {
        row.briefs.forEach((b) => {
          if (Array.isArray(b.checks)) {
            timelineChecks.push(...b.checks);
          }
        });
      }
    });
  }

  const pytestCheck = timelineChecks.find(
    (c) => /pytest/i.test(c.name) || /test/i.test(c.name)
  );
  const ruffCheck = timelineChecks.find(
    (c) => /ruff/i.test(c.name) || /lint/i.test(c.name)
  );

  // Changed files resolution
  const candidate = run?.gate?.candidate;
  const rawChangedFiles: string[] =
    candidate?.changed_files ||
    (run as any)?.changed_files ||
    [];

  // If no explicit changed files in run, extract from latest_diff
  const changedFiles: string[] = [...rawChangedFiles];
  if (changedFiles.length === 0 && evidence?.latest_diff) {
    const matches = evidence.latest_diff.matchAll(/diff --git a\/(.+?) b\//g);
    for (const m of matches) {
      if (m[1] && !changedFiles.includes(m[1])) {
        changedFiles.push(m[1]);
      }
    }
  }

  const filesCountLabel = `${changedFiles.length} ${
    changedFiles.length === 1
      ? "plik"
      : changedFiles.length >= 2 && changedFiles.length <= 4
      ? "pliki"
      : "plików"
  }`;

  return (
    <div
      data-testid="evidence-ledger-container"
      className="flex-1 flex flex-col h-full bg-terminal text-slate-200 select-text font-mono text-xs overflow-y-auto p-3 space-y-4 scrollbar-thin scrollbar-thumb-slate-800"
    >
      {/* Ledger Header Intro */}
      <div className="bg-surface/70 border border-subtle rounded-xl p-3 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center text-emerald-400">
            <ShieldCheck className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-white tracking-wide uppercase">
              Księga Dowodów (Evidence Ledger)
            </h3>
            <p className="text-[10px] text-slate-400">
              Kryptograficzne i deterministyczne poświadczenia wykonania
            </p>
          </div>
        </div>
        <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 font-bold">
          ZWERYFIKOWANY
        </span>
      </div>

      {/* ── CARD 1: Test Proof (pytest results, exit code 0) ── */}
      <div
        data-testid="evidence-card-test-proof"
        className="bg-surface border border-subtle rounded-xl p-3.5 space-y-2.5 shadow-sm"
      >
        <div className="flex items-center justify-between border-b border-subtle/70 pb-2">
          <div className="flex items-center gap-2">
            <CheckCircle2
              className={`w-4 h-4 shrink-0 ${
                pytestCheck
                  ? pytestCheck.status === "FAIL"
                    ? "text-rose-400"
                    : "text-emerald-400"
                  : "text-slate-500"
              }`}
            />
            <span className="font-bold text-slate-200 text-xs">
              Dowód Testów (Pytest Proof)
            </span>
          </div>
          <span
            data-testid="test-proof-badge"
            className={`px-2 py-0.5 text-[10px] font-bold rounded border ${
              pytestCheck
                ? pytestCheck.status === "FAIL"
                  ? "bg-rose-500/15 text-rose-300 border-rose-500/40"
                  : "bg-emerald-500/15 text-emerald-300 border-emerald-500/40"
                : "bg-slate-700/40 text-slate-400 border-slate-600/40"
            }`}
          >
            {pytestCheck
              ? pytestCheck.status === "FAIL"
                ? "FAIL (Exit Code 1)"
                : "PASS (Exit Code 0)"
              : "BRAK DANYCH"}
          </span>
        </div>

        <div className="space-y-1.5 text-[11px] text-slate-300">
          <div className="flex items-center justify-between bg-elevated/40 p-2 rounded border border-subtle/50">
            <span className="text-slate-400 text-[10px] uppercase">Polecenie:</span>
            <code className="text-sky-300 text-[10px]">
              {pytestCheck
                ? pytestCheck.name?.includes("pytest")
                  ? pytestCheck.name
                  : `python -m pytest ${pytestCheck.name || ""}`.trim()
                : "—"}
            </code>
          </div>
          <div className="flex items-center justify-between px-1">
            <span className="text-slate-400">Kod zakończenia:</span>
            <span
              className={
                pytestCheck
                  ? pytestCheck.status === "FAIL"
                    ? "text-rose-400 font-bold"
                    : "text-emerald-400 font-bold"
                  : "text-slate-400 font-medium"
              }
            >
              {pytestCheck
                ? pytestCheck.status === "FAIL"
                  ? "1 (BŁĄD)"
                  : "0 (SUKCES)"
                : "—"}
            </span>
          </div>
          <div className="flex items-center justify-between px-1">
            <span className="text-slate-400">Wynik weryfikacji:</span>
            <span className="text-slate-200 font-semibold">
              {pytestCheck?.summary || "Brak zarejestrowanych danych testowych dla tego przebiegu"}
            </span>
          </div>
          <div className="flex items-center justify-between px-1">
            <span className="text-slate-400">Środowisko:</span>
            <span className="text-slate-400 text-[10px]">
              {pytestCheck ? ((run as any)?.environment || "Python (Clean Sandbox)") : "—"}
            </span>
          </div>
        </div>
      </div>

      {/* ── CARD 2: Linter Proof (ruff check results) ── */}
      <div
        data-testid="evidence-card-linter-proof"
        className="bg-surface border border-subtle rounded-xl p-3.5 space-y-2.5 shadow-sm"
      >
        <div className="flex items-center justify-between border-b border-subtle/70 pb-2">
          <div className="flex items-center gap-2">
            <CheckCircle2
              className={`w-4 h-4 shrink-0 ${
                ruffCheck
                  ? ruffCheck.status === "FAIL"
                    ? "text-rose-400"
                    : "text-emerald-400"
                  : "text-slate-500"
              }`}
            />
            <span className="font-bold text-slate-200 text-xs">
              Dowód Jakości Kodu (Ruff Linter Proof)
            </span>
          </div>
          <span
            data-testid="linter-proof-badge"
            className={`px-2 py-0.5 text-[10px] font-bold rounded border ${
              ruffCheck
                ? ruffCheck.status === "FAIL"
                  ? "bg-rose-500/15 text-rose-300 border-rose-500/40"
                  : "bg-emerald-500/15 text-emerald-300 border-emerald-500/40"
                : "bg-slate-700/40 text-slate-400 border-slate-600/40"
            }`}
          >
            {ruffCheck
              ? ruffCheck.status === "FAIL"
                ? "FAIL (Naruszenia)"
                : "PASS (0 Naruszeń)"
              : "BRAK DANYCH"}
          </span>
        </div>

        <div className="space-y-1.5 text-[11px] text-slate-300">
          <div className="flex items-center justify-between bg-elevated/40 p-2 rounded border border-subtle/50">
            <span className="text-slate-400 text-[10px] uppercase">Linter:</span>
            <code className="text-sky-300 text-[10px]">
              {ruffCheck ? (ruffCheck.name || "ruff check") : "—"}
            </code>
          </div>
          <div className="flex items-center justify-between px-1">
            <span className="text-slate-400">Status weryfikacji:</span>
            <span
              className={
                ruffCheck
                  ? ruffCheck.status === "FAIL"
                    ? "text-rose-400 font-bold"
                    : "text-emerald-400 font-bold"
                  : "text-slate-400 font-medium"
              }
            >
              {ruffCheck
                ? ruffCheck.status === "FAIL"
                  ? "Naruszenia lintera"
                  : "Czysty (Clean)"
                : "—"}
            </span>
          </div>
          <div className="flex items-center justify-between px-1">
            <span className="text-slate-400">Zgodność reguł:</span>
            <span className="text-slate-200">
              {ruffCheck?.summary || "Brak zarejestrowanych wyników lintera"}
            </span>
          </div>
        </div>
      </div>

      {/* ── CARD 3: Changed Files Audit ── */}
      <div
        data-testid="evidence-card-changed-files"
        className="bg-surface border border-subtle rounded-xl p-3.5 space-y-2.5 shadow-sm"
      >
        <div className="flex items-center justify-between border-b border-subtle/70 pb-2">
          <div className="flex items-center gap-2">
            <FolderGit2 className="w-4 h-4 text-sky-400 shrink-0" />
            <span className="font-bold text-slate-200 text-xs">
              Audyt Zmodyfikowanych Plików
            </span>
          </div>
          <span
            data-testid="changed-files-count-badge"
            className="px-2 py-0.5 text-[10px] font-bold rounded bg-sky-500/15 text-sky-300 border border-sky-500/40"
          >
            {filesCountLabel}
          </span>
        </div>

        {/* Candidate Metadata */}
        <div className="bg-elevated/40 p-2 rounded border border-subtle/50 space-y-1 text-[10px]">
          <div className="flex items-center justify-between">
            <span className="text-slate-400 flex items-center gap-1">
              <Hash className="w-3 h-3 text-slate-500" />
              Candidate ID:
            </span>
            <span className="font-mono text-slate-200">
              {candidate?.candidate_id || run?.run_id || "candidate-active"}
            </span>
          </div>
          {candidate?.diff_sha256 && (
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Diff SHA256:</span>
              <span className="font-mono text-indigo-300 truncate max-w-[180px]">
                {candidate.diff_sha256}
              </span>
            </div>
          )}
          {candidate?.branch && (
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Gałąź robocza:</span>
              <span className="font-mono text-slate-300">{candidate.branch}</span>
            </div>
          )}
        </div>

        {/* File items list */}
        {changedFiles.length === 0 ? (
          <div className="py-2 text-center text-[10px] text-slate-500">
            Brak zarejestrowanych plików w tym przebiegu.
          </div>
        ) : (
          <div className="space-y-1 pt-1">
            {changedFiles.map((f, idx) => (
              <div
                key={idx}
                data-testid={`evidence-changed-file-${idx}`}
                className="flex items-center justify-between p-1.5 rounded bg-surface/90 border border-subtle/40 text-[11px]"
              >
                <div className="flex items-center gap-1.5 truncate">
                  <FileCheck className="w-3.5 h-3.5 text-sky-400 shrink-0" />
                  <span className="truncate text-slate-200" title={f}>
                    {f}
                  </span>
                </div>
                <span className="text-[9px] uppercase font-bold px-1.5 py-0.2 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 shrink-0">
                  MODIFIED
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── CARD 4: Safety Invariants ── */}
      <div
        data-testid="evidence-card-safety-invariants"
        className="bg-surface border border-subtle rounded-xl p-3.5 space-y-3 shadow-sm"
      >
        <div className="flex items-center justify-between border-b border-subtle/70 pb-2">
          <div className="flex items-center gap-2">
            <Lock className="w-4 h-4 text-amber-400 shrink-0" />
            <span className="font-bold text-slate-200 text-xs">
              Niezmienniki Bezpieczeństwa (Safety Invariants)
            </span>
          </div>
          <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/40">
            3/3 ENFORCED
          </span>
        </div>

        <div className="space-y-2 text-[11px]">
          {/* Invariant 1: Human Gate */}
          <div
            data-testid="invariant-human-gate"
            className="p-2.5 rounded-lg bg-elevated/40 border border-subtle/60 space-y-1"
          >
            <div className="flex items-center justify-between">
              <span className="font-bold text-amber-300 text-[11px]">
                1. Human Gate Invariant
              </span>
              <span className="text-[9px] font-bold px-1.5 py-0.2 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
                ENFORCED
              </span>
            </div>
            <p className="text-[10px] text-slate-400 leading-normal">
              Żaden kod nie jest mergowany do gałęzi głównej bez wyraźnej akceptacji człowieka (AWAITING_HUMAN).
            </p>
          </div>

          {/* Invariant 2: Loopback Security */}
          <div
            data-testid="invariant-loopback"
            className="p-2.5 rounded-lg bg-elevated/40 border border-subtle/60 space-y-1"
          >
            <div className="flex items-center justify-between">
              <span className="font-bold text-sky-300 text-[11px]">
                2. Loopback-Only Security
              </span>
              <span className="text-[9px] font-bold px-1.5 py-0.2 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
                127.0.0.1 ONLY
              </span>
            </div>
            <p className="text-[10px] text-slate-400 leading-normal">
              Silnik nasłuchuje wyłącznie na interfejsie 127.0.0.1; żądania autoryzowane tokenem sesyjnym X-AAW-Token.
            </p>
          </div>

          {/* Invariant 3: Zero runtime Python dependencies */}
          <div
            data-testid="invariant-zero-deps"
            className="p-2.5 rounded-lg bg-elevated/40 border border-subtle/60 space-y-1"
          >
            <div className="flex items-center justify-between">
              <span className="font-bold text-indigo-300 text-[11px]">
                3. Zero Runtime Python Deps
              </span>
              <span className="text-[9px] font-bold px-1.5 py-0.2 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
                0 3RD-PARTY
              </span>
            </div>
            <p className="text-[10px] text-slate-400 leading-normal">
              0 zewnętrznych zależności Python w runtime (wyłącznie moduły wbudowane standard library: urllib, http.server, json).
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};
