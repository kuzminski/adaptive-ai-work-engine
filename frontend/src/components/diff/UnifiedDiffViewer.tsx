/**
 * AAW Agentic Studio — Unified Diff Viewer Component
 *
 * Implements a full-fidelity code diff inspector with:
 * - Line-by-line diff parser (additions, deletions, chunks, headers)
 * - Color-coded syntax highlights (green +, red -, purple @@)
 * - Line number gutters (old & new line numbers)
 * - Multi-file selector chips / tabs with additions/deletions counts
 * - Diff truncation banner for diffs > 200 KB or explicit truncation flags
 * - Copy to clipboard & file navigation
 */

import React, { useMemo, useState } from "react";
import {
  FileCode,
  AlertTriangle,
  Copy,
  Check,
  Split,
  Layers,
  FileCheck,
} from "lucide-react";
import { EvidenceView, RunDetail } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";

export interface DiffLine {
  type: "add" | "delete" | "chunk" | "header" | "context";
  content: string;
  oldLineNumber?: number;
  newLineNumber?: number;
}

export interface DiffFile {
  id: string;
  fileName: string;
  filePath: string;
  oldPath: string;
  newPath: string;
  additions: number;
  deletions: number;
  lines: DiffLine[];
}

export interface UnifiedDiffViewerProps {
  run?: RunDetail | null;
  evidence?: EvidenceView | null;
}

const MAX_DIFF_BYTES = 200 * 1024; // 200 KB limit

/**
 * Parses raw unified diff text into structured DiffFile and DiffLine records
 */
export function parseUnifiedDiff(rawDiff: string, defaultFiles: string[] = []): DiffFile[] {
  if (!rawDiff || !rawDiff.trim()) {
    if (defaultFiles.length > 0) {
      return defaultFiles.map((file, idx) => ({
        id: `file-${idx}`,
        fileName: file.split(/[\\/]/).pop() || file,
        filePath: file,
        oldPath: file,
        newPath: file,
        additions: 0,
        deletions: 0,
        lines: [],
      }));
    }
    return [];
  }

  const lines = rawDiff.split("\n");
  const files: DiffFile[] = [];

  let currentFile: DiffFile | null = null;
  let oldLine = 0;
  let newLine = 0;

  const pushCurrent = () => {
    if (currentFile) {
      files.push(currentFile);
      currentFile = null;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Detect file boundary
    if (line.startsWith("diff --git ")) {
      pushCurrent();
      const parts = line.split(" ");
      const rawPathB = parts[3] ? parts[3].replace(/^b\//, "") : "";
      const rawPathA = parts[2] ? parts[2].replace(/^a\//, "") : "";
      const path = rawPathB || rawPathA || `file-${files.length + 1}`;
      const name = path.split(/[\\/]/).pop() || path;

      currentFile = {
        id: `diff-file-${files.length}`,
        fileName: name,
        filePath: path,
        oldPath: rawPathA,
        newPath: rawPathB,
        additions: 0,
        deletions: 0,
        lines: [{ type: "header", content: line }],
      };
      continue;
    }

    if (!currentFile) {
      // Start of diff without explicit "diff --git" header
      const defaultName = defaultFiles[0] || "changes.patch";
      currentFile = {
        id: `diff-file-0`,
        fileName: defaultName.split(/[\\/]/).pop() || defaultName,
        filePath: defaultName,
        oldPath: defaultName,
        newPath: defaultName,
        additions: 0,
        deletions: 0,
        lines: [],
      };
    }

    // Header lines
    if (
      line.startsWith("--- ") ||
      line.startsWith("+++ ") ||
      line.startsWith("index ") ||
      line.startsWith("new file mode") ||
      line.startsWith("deleted file mode") ||
      line.startsWith("similarity index")
    ) {
      // Extract cleaner path if available from ---/+++
      if (line.startsWith("+++ b/")) {
        const p = line.substring(6).trim();
        if (p) {
          currentFile.filePath = p;
          currentFile.fileName = p.split(/[\\/]/).pop() || p;
        }
      }
      currentFile.lines.push({ type: "header", content: line });
      continue;
    }

    // Chunk header @@ -old,count +new,count @@
    if (line.startsWith("@@")) {
      const match = line.match(/@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
      if (match) {
        oldLine = parseInt(match[1], 10);
        newLine = parseInt(match[2], 10);
      }
      currentFile.lines.push({
        type: "chunk",
        content: line,
      });
      continue;
    }

    // Additions
    if (line.startsWith("+")) {
      currentFile.additions++;
      currentFile.lines.push({
        type: "add",
        content: line,
        newLineNumber: newLine++,
      });
      continue;
    }

    // Deletions
    if (line.startsWith("-")) {
      currentFile.deletions++;
      currentFile.lines.push({
        type: "delete",
        content: line,
        oldLineNumber: oldLine++,
      });
      continue;
    }

    // Context / unchanged
    currentFile.lines.push({
      type: "context",
      content: line,
      oldLineNumber: oldLine > 0 ? oldLine++ : undefined,
      newLineNumber: newLine > 0 ? newLine++ : undefined,
    });
  }

  pushCurrent();
  return files;
}

export const UnifiedDiffViewer: React.FC<UnifiedDiffViewerProps> = ({
  run: propRun,
  evidence: propEvidence,
}) => {
  const store = useEngineStore();
  const run = propRun !== undefined ? propRun : store.run;
  const evidence = propEvidence !== undefined ? propEvidence : store.evidence;

  const rawDiff = evidence?.latest_diff || "";
  const changedFiles =
    run?.gate?.candidate?.changed_files ||
    (run as any)?.changed_files ||
    [];

  // Truncation detection
  const isTruncated =
    rawDiff.length > MAX_DIFF_BYTES ||
    rawDiff.includes("[TRUNCATED]") ||
    rawDiff.includes("[DIFF_TRUNCATED]") ||
    Boolean((evidence as any)?.diff_truncated) ||
    Boolean(evidence?.descriptor?.diff_truncated);

  // Parse files
  const parsedFiles = useMemo(
    () => parseUnifiedDiff(rawDiff, changedFiles),
    [rawDiff, changedFiles]
  );

  const [selectedFileId, setSelectedFileId] = useState<string>("all");
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    if (!rawDiff) return;
    navigator.clipboard.writeText(rawDiff).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  const activeFiles =
    selectedFileId === "all"
      ? parsedFiles
      : parsedFiles.filter((f) => f.id === selectedFileId);

  const totalAdditions = parsedFiles.reduce((acc, f) => acc + f.additions, 0);
  const totalDeletions = parsedFiles.reduce((acc, f) => acc + f.deletions, 0);

  if (!rawDiff && changedFiles.length === 0) {
    return (
      <div
        data-testid="diff-empty-state"
        className="flex-1 flex flex-col items-center justify-center p-8 text-center text-slate-500 font-mono text-xs bg-terminal"
      >
        <div className="w-12 h-12 rounded-xl bg-surface border border-subtle flex items-center justify-center mb-3 text-slate-600">
          <FileCode className="w-6 h-6" />
        </div>
        <p className="text-slate-300 font-semibold mb-1">Brak zmian w kodzie</p>
        <p className="text-[11px] text-slate-500 max-w-xs">
          Unified Diff jest pusty lub silnik nie wygenerował jeszcze modyfikacji plików.
        </p>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col h-full bg-terminal text-slate-200 select-text font-mono text-xs overflow-hidden">
      {/* Truncation banner */}
      {isTruncated && (
        <div
          data-testid="diff-truncation-banner"
          className="px-3 py-2 bg-amber-500/15 border-b border-amber-500/30 text-amber-300 text-[11px] flex items-center justify-between shrink-0"
        >
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
            <span>
              Diff przekracza limit 200 KB i został obcięty dla zachowania wysokiej wydajności.
            </span>
          </div>
          <span className="text-[10px] uppercase font-bold px-1.5 py-0.5 rounded bg-amber-500/20 border border-amber-500/40">
            TRUNCATED
          </span>
        </div>
      )}

      {/* Header bar: File selector & Stats */}
      <div
        className="px-3 py-2 bg-surface/90 border-b border-subtle flex flex-col gap-2 shrink-0 select-none"
        data-testid="diff-viewer-header"
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Split className="w-3.5 h-3.5 text-sky-400" />
            <span className="text-[11px] font-semibold text-slate-300">
              Zmodyfikowane pliki ({parsedFiles.length})
            </span>
            <div className="flex items-center gap-1.5 text-[10px] font-mono ml-2">
              <span className="text-emerald-400 font-bold">+{totalAdditions}</span>
              <span className="text-slate-600">/</span>
              <span className="text-rose-400 font-bold">-{totalDeletions}</span>
            </div>
          </div>

          {/* Copy diff button */}
          <button
            onClick={handleCopy}
            data-testid="diff-copy-button"
            className="px-2 py-1 bg-elevated hover:bg-slate-700 text-slate-300 hover:text-white rounded border border-subtle flex items-center gap-1.5 text-[10px] transition"
          >
            {copied ? (
              <>
                <Check className="w-3 h-3 text-emerald-400" />
                <span className="text-emerald-300">Skopiowano</span>
              </>
            ) : (
              <>
                <Copy className="w-3 h-3" />
                <span>Kopiuj Diff</span>
              </>
            )}
          </button>
        </div>

        {/* File selector chips */}
        {parsedFiles.length > 1 && (
          <div className="flex items-center gap-1.5 overflow-x-auto pb-1 scrollbar-thin">
            <button
              onClick={() => setSelectedFileId("all")}
              data-testid="diff-file-chip-all"
              className={`px-2.5 py-1 rounded text-[10px] font-mono shrink-0 transition flex items-center gap-1.5 ${
                selectedFileId === "all"
                  ? "bg-phase-execute text-sky-950 font-bold"
                  : "bg-surface hover:bg-elevated text-slate-400 border border-subtle"
              }`}
            >
              <Layers className="w-3 h-3" />
              <span>Wszystkie ({parsedFiles.length})</span>
            </button>

            {parsedFiles.map((file, idx) => (
              <button
                key={file.id}
                onClick={() => setSelectedFileId(file.id)}
                data-testid={`diff-file-chip-${idx}`}
                title={file.filePath}
                className={`px-2.5 py-1 rounded text-[10px] font-mono shrink-0 transition flex items-center gap-1.5 ${
                  selectedFileId === file.id
                    ? "bg-phase-execute text-sky-950 font-bold"
                    : "bg-surface hover:bg-elevated text-slate-400 border border-subtle"
                }`}
              >
                <FileCode className="w-3 h-3" />
                <span className="max-w-[140px] truncate">{file.fileName}</span>
                <span className="text-[9px] opacity-80">
                  +{file.additions} -{file.deletions}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Diff Content Viewport */}
      <div
        data-testid="diff-viewport"
        className="flex-1 p-2 overflow-y-auto overflow-x-auto space-y-4 font-mono text-[11px] leading-relaxed scrollbar-thin scrollbar-thumb-slate-800"
      >
        {activeFiles.map((file) => (
          <div
            key={file.id}
            data-testid={`diff-file-section-${file.fileName}`}
            className="border border-subtle rounded-lg overflow-hidden bg-surface/50"
          >
            {/* File Header Bar */}
            <div className="px-3 py-1.5 bg-slate-800/80 border-b border-subtle flex items-center justify-between text-xs text-slate-300">
              <div className="flex items-center gap-2">
                <FileCheck className="w-3.5 h-3.5 text-sky-400" />
                <span className="font-semibold text-white">{file.filePath}</span>
              </div>
              <div className="flex items-center gap-2 text-[10px]">
                <span className="text-emerald-400 font-bold">+{file.additions}</span>
                <span className="text-rose-400 font-bold">-{file.deletions}</span>
              </div>
            </div>

            {/* Line-by-line render */}
            {file.lines.length === 0 ? (
              <div className="p-4 text-center text-slate-500 text-xs">
                Brak zarejestrowanych linii diffa dla tego pliku.
              </div>
            ) : (
              <div className="divide-y divide-subtle/30 font-mono text-[11px]">
                {file.lines.map((line, lIdx) => {
                  let rowBg = "";
                  let textCol = "text-slate-300";
                  let prefixSymbol = " ";

                  if (line.type === "header") {
                    rowBg = "bg-slate-900/90 text-sky-400 font-semibold";
                    prefixSymbol = "";
                  } else if (line.type === "chunk") {
                    rowBg = "bg-purple-950/30 text-purple-300 font-semibold";
                    prefixSymbol = "";
                  } else if (line.type === "add") {
                    rowBg = "bg-emerald-950/30 text-emerald-300";
                    prefixSymbol = "+";
                  } else if (line.type === "delete") {
                    rowBg = "bg-rose-950/30 text-rose-300";
                    prefixSymbol = "-";
                  }

                  return (
                    <div
                      key={lIdx}
                      data-testid={`diff-line-${line.type}`}
                      className={`flex items-start hover:bg-slate-800/50 transition-colors ${rowBg}`}
                    >
                      {/* Old line gutter */}
                      <span className="w-9 px-1 text-right text-slate-600 select-none shrink-0 font-mono text-[10px]">
                        {line.oldLineNumber !== undefined ? line.oldLineNumber : ""}
                      </span>

                      {/* New line gutter */}
                      <span className="w-9 px-1 text-right text-slate-600 select-none shrink-0 font-mono text-[10px] border-r border-subtle/50">
                        {line.newLineNumber !== undefined ? line.newLineNumber : ""}
                      </span>

                      {/* Prefix sign */}
                      <span className="w-4 text-center select-none shrink-0 font-bold text-slate-500">
                        {prefixSymbol}
                      </span>

                      {/* Code Content */}
                      <pre className={`flex-1 overflow-x-auto whitespace-pre-wrap break-all pr-2 ${textCol}`}>
                        {line.content.replace(/^[+-]/, "")}
                      </pre>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};
