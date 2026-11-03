/**
 * AAW Agentic Studio — Universal MCP Config Modal
 *
 * Provides a clean modal dialog to inspect configuration status across
 * Antigravity, Claude, and Codex, with 1-click automated setup execution.
 */

import React, { useState, useEffect } from "react";
import {
  X,
  Zap,
  CheckCircle2,
  AlertCircle,
  Terminal,
  Folder,
  Loader2,
  Cpu,
  Bot,
  Sparkles,
} from "lucide-react";
import { useEngineStore } from "../../store/engineStore";

export interface MCPConfigModalProps {
  isOpen?: boolean;
  onClose?: () => void;
}

export const MCPConfigModal: React.FC<MCPConfigModalProps> = ({
  isOpen,
  onClose,
}) => {
  const {
    mcpStatus,
    mcpModalOpen,
    setMCPModalOpen,
    configureMCP,
    loadMCPStatus,
  } = useEngineStore();

  const show = isOpen !== undefined ? isOpen : mcpModalOpen;
  const handleClose = onClose || (() => setMCPModalOpen(false));

  const [configuringTarget, setConfiguringTarget] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{
    type: "success" | "error";
    message: string;
  } | null>(null);

  // Close on Escape key
  useEffect(() => {
    if (!show) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handleClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [show, handleClose]);

  if (!show) return null;

  const handleConfigure = async (target: string = "all") => {
    setConfiguringTarget(target);
    setFeedback(null);
    try {
      const res = await configureMCP({ target, install: true });
      setFeedback({
        type: "success",
        message:
          target === "all"
            ? "Pomyślnie skonfigurowano MCP dla wszystkich klientów (Antigravity, Claude, Codex)!"
            : `Pomyślnie skonfigurowano klienta MCP: ${res.target || target}!`,
      });
      await loadMCPStatus();
    } catch (err: any) {
      setFeedback({
        type: "error",
        message: err?.message || `Nie udało się skonfigurować MCP dla ${target}`,
      });
    } finally {
      setConfiguringTarget(null);
    }
  };

  const clients = [
    {
      id: "antigravity",
      name: "Antigravity CLI & IDE",
      icon: Sparkles,
      configured: Boolean(mcpStatus?.antigravity?.configured),
      path: mcpStatus?.antigravity?.path || "~/.gemini/antigravity/mcp_config.json",
      detail: "Google Antigravity MCP integration",
    },
    {
      id: "claude",
      name: "Claude Code & Desktop",
      icon: Bot,
      configured: Boolean(mcpStatus?.claude?.configured),
      path:
        mcpStatus?.claude?.desktop_path ||
        mcpStatus?.claude?.code_path ||
        "~/.claude/claude_desktop_config.json",
      detail: "Anthropic Claude Desktop & CLI harness",
    },
    {
      id: "codex",
      name: "Codex CLI",
      icon: Cpu,
      configured: Boolean(mcpStatus?.codex?.configured),
      path: mcpStatus?.codex?.path || "~/.codex/config.toml",
      detail: "OpenAI Codex CLI execution harness",
    },
  ];

  const tools = [
    { name: "aaw_detect_providers", desc: "Wykrywanie CLI i stan logowania" },
    { name: "aaw_inspect_repo", desc: "Inspekcja stanu Git i dirty worktree" },
    { name: "aaw_preview_task", desc: "Podgląd mandatu i planu zadań" },
    { name: "aaw_start_task", desc: "Uruchamianie autonomicznego zadania" },
    { name: "aaw_run_status", desc: "Monitorowanie 7-fazowej maszyny stanów" },
    { name: "aaw_run_evidence", desc: "Dostęp do diffów, logów i dowodów" },
    { name: "aaw_run_stop", desc: "Bezpieczne zatrzymanie przebiegu" },
    { name: "aaw_run_resume", desc: "Wznowienie pracy z tokenem blokady" },
  ];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in"
      onClick={handleClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="mcp-modal-title"
    >
      <div
        className="bg-surface border border-subtle rounded-xl shadow-2xl max-w-xl w-full max-h-[90vh] flex flex-col overflow-hidden text-slate-100"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Modal Header */}
        <div className="flex items-center justify-between p-5 border-b border-subtle bg-elevated/40">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-phase-execute/10 border border-phase-execute/30 rounded-lg text-phase-execute">
              <Zap className="w-5 h-5 text-sky-400" />
            </div>
            <div>
              <h2 id="mcp-modal-title" className="text-lg font-bold text-white tracking-tight">
                Universal MCP Server Hub
              </h2>
              <p className="text-xs text-slate-400">
                Połącz agentów Claude, Codex i Antigravity z silnikiem AAW przez protokół MCP
              </p>
            </div>
          </div>
          <button
            onClick={handleClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-elevated transition"
            aria-label="Zamknij"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-5 space-y-5 overflow-y-auto">
          {/* Feedback Banner */}
          {feedback && (
            <div
              className={`p-3.5 rounded-lg border text-xs flex items-center justify-between ${
                feedback.type === "success"
                  ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-300"
                  : "bg-rose-500/10 border-rose-500/30 text-rose-300"
              }`}
            >
              <div className="flex items-center gap-2">
                {feedback.type === "success" ? (
                  <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
                ) : (
                  <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
                )}
                <span>{feedback.message}</span>
              </div>
              <button
                onClick={() => setFeedback(null)}
                className="text-slate-400 hover:text-white ml-2 text-xs font-mono"
              >
                ✕
              </button>
            </div>
          )}

          {/* Client status list */}
          <div>
            <div className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2.5">
              Stan integracji klientów AI
            </div>
            <div className="space-y-2.5">
              {clients.map((client) => {
                const Icon = client.icon;
                const isTargetConfiguring = configuringTarget === client.id;

                return (
                  <div
                    key={client.id}
                    className="p-3 bg-elevated/50 border border-subtle rounded-lg flex items-center justify-between gap-3 hover:border-slate-700 transition"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="p-2 bg-surface rounded-md border border-subtle shrink-0">
                        <Icon className="w-4 h-4 text-slate-300" />
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-semibold text-slate-100 truncate">
                            {client.name}
                          </span>
                          <span
                            className={`px-2 py-0.5 text-[10px] font-mono font-bold rounded-full border ${
                              client.configured
                                ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                                : "bg-slate-800 text-slate-400 border-slate-700"
                            }`}
                          >
                            {client.configured ? "SKONFIGUROWANY" : "DO SKONFIGUROWANIA"}
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5 text-[11px] text-slate-400 font-mono truncate mt-0.5">
                          <Folder className="w-3 h-3 text-slate-500 shrink-0" />
                          <span className="truncate">{client.path}</span>
                        </div>
                      </div>
                    </div>

                    <button
                      onClick={() => handleConfigure(client.id)}
                      disabled={configuringTarget !== null}
                      className={`px-3 py-1.5 text-xs font-medium rounded border transition shrink-0 flex items-center gap-1.5 ${
                        client.configured
                          ? "bg-surface hover:bg-elevated border-subtle text-slate-300"
                          : "bg-sky-500/20 hover:bg-sky-500/30 border-sky-500/40 text-sky-200 font-semibold"
                      } disabled:opacity-50`}
                    >
                      {isTargetConfiguring ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <Zap className="w-3.5 h-3.5 text-sky-400" />
                      )}
                      <span>{client.configured ? "Zaktualizuj" : "Konfiguruj"}</span>
                    </button>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Tools Info Box */}
          <div className="p-3.5 bg-terminal border border-subtle rounded-lg">
            <div className="flex items-center justify-between mb-2 pb-1.5 border-b border-subtle/50">
              <div className="flex items-center gap-1.5 text-xs font-mono font-semibold text-slate-300">
                <Terminal className="w-3.5 h-3.5 text-sky-400" />
                <span>8 Dostępnych Narzędzi MCP (Tools)</span>
              </div>
              <span className="text-[11px] font-mono text-emerald-400 font-bold">100% Loopback Only</span>
            </div>
            <div className="grid grid-cols-2 gap-2 text-[11px] font-mono text-slate-400">
              {tools.map((tool) => (
                <div key={tool.name} className="flex flex-col">
                  <span className="text-sky-300 font-semibold">{tool.name}</span>
                  <span className="text-slate-500 text-[10px]">{tool.desc}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Modal Footer */}
        <div className="p-4 border-t border-subtle bg-elevated/30 flex items-center justify-between">
          <div className="text-xs text-slate-400">
            Automatyczna edycja plików konfiguracyjnych JSON/TOML
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleClose}
              className="px-4 py-2 text-xs font-medium text-slate-300 hover:text-white bg-surface hover:bg-elevated border border-subtle rounded-lg transition"
            >
              Zamknij
            </button>
            <button
              onClick={() => handleConfigure("all")}
              disabled={configuringTarget !== null}
              className="px-4 py-2 text-xs font-bold text-sky-950 bg-sky-400 hover:bg-sky-300 rounded-lg shadow-lg shadow-sky-950/50 flex items-center gap-1.5 transition disabled:opacity-50"
            >
              {configuringTarget === "all" ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Zap className="w-4 h-4 fill-sky-950" />
              )}
              <span>⚡ Konfiguruj wszystko (1-Click)</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
