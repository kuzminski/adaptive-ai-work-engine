/**
 * AAW Agentic Studio — Responsive Tri-Pane Studio Layout
 *
 * Implements the core Command-Center shell:
 * - Top Header: Brand logo, Release version pill, Active repo & Git branch badge,
 *   Active run status capsule, and Polling heartbeat.
 * - Left Rail: Navigation, AI Provider Health, Autonomy Presets, MCP Hub.
 * - Center Canvas: Dynamic execution graph, task details, decision gates.
 * - Right Drawer: Telemetry terminal, Unified diff inspector, Evidence ledger.
 */

import React from "react";
import {
  FolderGit2,
  GitBranch,
  Radio,
  ShieldAlert,
  CheckCircle2,
  PauseCircle,
  PanelRight,
} from "lucide-react";
import { useEngineStore } from "../../store/engineStore";
import { LeftRail } from "../nav/LeftRail";
import { MCPConfigModal } from "../mcp/MCPConfigModal";

export interface StudioLayoutProps {
  children?: React.ReactNode;
  leftRail?: React.ReactNode;
  centerCanvas?: React.ReactNode;
  rightDrawer?: React.ReactNode;
}

export const StudioLayout: React.FC<StudioLayoutProps> = ({
  children,
  leftRail,
  centerCanvas,
  rightDrawer,
}) => {
  const {
    bootstrap,
    run,
    activeRunId,
    pollingActive,
    rightDrawerOpen,
    toggleRightDrawer,
  } = useEngineStore();

  // Version resolution
  const releaseVersion =
    bootstrap?.version ||
    (bootstrap?.release?.release ? `v${bootstrap.release.release}` : "v0.4.0");

  // Repository & Branch resolution
  const rawRepoPath =
    run?.workspace?.repo_path ||
    (bootstrap?.recent_repos && bootstrap.recent_repos.length > 0
      ? bootstrap.recent_repos[0]
      : "adaptive-ai-work-engine");
  const repoName = rawRepoPath.split(/[\\/]/).filter(Boolean).pop() || rawRepoPath;
  const branchName = run?.workspace?.branch || "main";

  // Run status capsule resolution
  const getRunStatusCapsule = () => {
    if (!run && !activeRunId) {
      return {
        label: "IDLE — Oczekiwanie na zadanie",
        className: "bg-slate-800 text-slate-400 border-slate-700",
        icon: <span className="w-2 h-2 rounded-full bg-slate-500" />,
      };
    }

    const engineStatus = run?.engine?.status;
    const runStatus = run?.status;

    if (
      engineStatus === "AWAITING_HUMAN" ||
      runStatus === "READY_FOR_DECISION" ||
      runStatus === "NEEDS_ATTENTION"
    ) {
      return {
        label: "BRAMKA DECYZYJNA (Wymaga człowieka)",
        className:
          "bg-amber-500/10 text-amber-300 border-amber-500/40 font-bold shadow-[0_0_12px_rgba(245,158,11,0.2)]",
        icon: <ShieldAlert className="w-3.5 h-3.5 text-amber-400 animate-pulse" />,
      };
    }

    if (runStatus === "RUNNING") {
      const phase = run?.process?.phase ? ` [${run.process.phase}]` : "";
      const iteration = run?.process?.iteration
        ? ` (Iteracja ${run.process.iteration}/${run.process.max_iterations || "?"})`
        : "";
      return {
        label: `AKTYWNY${phase}${iteration}`,
        className: "bg-sky-500/10 text-sky-400 border-sky-500/30 font-semibold",
        icon: <span className="w-2 h-2 rounded-full bg-sky-400 animate-pulse" />,
      };
    }

    if (
      engineStatus === "HUMAN_APPROVED" ||
      engineStatus === "PROMOTED" ||
      runStatus === "ACCEPTED"
    ) {
      return {
        label: "ZAAKCEPTOWANY / ZAKOŃCZONY",
        className: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30 font-semibold",
        icon: <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />,
      };
    }

    if (runStatus === "PAUSED" || runStatus === "INTERRUPTED") {
      return {
        label: "WSTRZYMANY (PAUSED)",
        className: "bg-slate-800 text-slate-300 border-slate-700",
        icon: <PauseCircle className="w-3.5 h-3.5 text-slate-400" />,
      };
    }

    return {
      label: runStatus || "STATUS PRZEBIEGU",
      className: "bg-slate-800 text-slate-300 border-slate-700",
      icon: <span className="w-2 h-2 rounded-full bg-slate-400" />,
    };
  };

  const statusCapsule = getRunStatusCapsule();

  return (
    <div className="h-screen w-screen bg-base text-slate-100 flex flex-col overflow-hidden font-sans select-none">
      {/* Top Header */}
      <header
        className="h-14 bg-surface border-b border-subtle px-4 flex items-center justify-between shrink-0 z-20"
        data-testid="studio-header"
      >
        {/* Left: Brand logo & Title & Release Pill */}
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-lg overflow-hidden flex items-center justify-center bg-blue-600/20 border border-blue-500/30 shrink-0">
              <img
                src="/static/icon.svg"
                alt="AAW Logo"
                className="w-6 h-6 object-contain"
                onError={(e) => {
                  (e.target as HTMLElement).style.display = "none";
                }}
              />
            </div>
            <h1 className="text-sm font-bold tracking-tight text-white flex items-center gap-2">
              <span>AAW Agentic Studio</span>
            </h1>
          </div>

          <span
            data-testid="release-version-pill"
            className="px-2 py-0.5 text-[10px] font-mono font-semibold rounded-full bg-elevated border border-subtle text-slate-300"
          >
            {releaseVersion}
          </span>
        </div>

        {/* Center: Active Repo & Git Branch Badge */}
        <div
          data-testid="active-repo-badge"
          className="hidden md:flex items-center gap-2 px-3 py-1 bg-elevated/70 border border-subtle rounded-lg text-xs font-mono text-slate-300"
        >
          <FolderGit2 className="w-3.5 h-3.5 text-sky-400 shrink-0" />
          <span className="truncate max-w-[220px] text-slate-200 font-semibold" title={rawRepoPath}>
            {repoName}
          </span>
          <span className="text-slate-600">/</span>
          <div className="flex items-center gap-1 text-slate-300">
            <GitBranch className="w-3.5 h-3.5 text-indigo-400 shrink-0" />
            <span data-testid="git-branch-name" className="font-semibold text-indigo-300">
              {branchName}
            </span>
          </div>
        </div>

        {/* Right: Active Run Status Capsule & Polling & Drawer Toggle */}
        <div className="flex items-center gap-3">
          {/* Active run status capsule */}
          <div
            data-testid="run-status-capsule"
            className={`px-3 py-1 text-xs font-mono rounded-lg border flex items-center gap-2 ${statusCapsule.className}`}
          >
            {statusCapsule.icon}
            <span className="truncate max-w-[240px]">{statusCapsule.label}</span>
          </div>

          {/* Polling Heartbeat */}
          <div
            data-testid="polling-indicator"
            className={`hidden sm:flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-mono rounded-lg border ${
              pollingActive
                ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                : "bg-slate-900 text-slate-500 border-slate-800"
            }`}
          >
            <Radio className={`w-3 h-3 ${pollingActive ? "animate-pulse text-emerald-400" : ""}`} />
            <span>{pollingActive ? "LIVE" : "IDLE"}</span>
          </div>

          {/* Drawer Toggle Button */}
          {rightDrawer && (
            <button
              onClick={toggleRightDrawer}
              data-testid="toggle-right-drawer"
              title={rightDrawerOpen ? "Zwiń boczny panel telemetrii" : "Rozwiń panel telemetrii"}
              className={`p-1.5 rounded-lg border transition ${
                rightDrawerOpen
                  ? "bg-elevated border-subtle text-sky-400"
                  : "bg-surface border-subtle text-slate-400 hover:text-slate-200"
              }`}
            >
              <PanelRight className="w-4 h-4" />
            </button>
          )}
        </div>
      </header>

      {/* Tri-Pane Grid / Flex Body */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left Rail Slot (~270px) */}
        <div
          data-testid="left-rail-container"
          className="w-[270px] min-w-[270px] max-w-[270px] shrink-0 border-r border-subtle bg-surface flex flex-col overflow-y-auto"
        >
          {leftRail || <LeftRail />}
        </div>

        {/* Center Canvas Slot (Flexible) */}
        <main
          data-testid="center-canvas-container"
          className="flex-1 flex flex-col overflow-y-auto bg-base p-4 lg:p-6 relative"
        >
          {centerCanvas || children}
        </main>

        {/* Right Drawer Slot (Collapsible ~380-420px) */}
        {rightDrawer && (
          <aside
            data-testid="right-drawer-container"
            className={`shrink-0 border-l border-subtle bg-surface flex flex-col transition-all duration-200 ${
              rightDrawerOpen ? "w-[380px] lg:w-[420px]" : "w-0 overflow-hidden border-none"
            }`}
          >
            {rightDrawer}
          </aside>
        )}
      </div>

      {/* Universal MCP Config Modal */}
      <MCPConfigModal />
    </div>
  );
};
