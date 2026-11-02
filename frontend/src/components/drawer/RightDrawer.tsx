/**
 * AAW Agentic Studio — Right Drawer Component
 *
 * Houses the Telemetry Terminal, Unified Diff Viewer, and Evidence Ledger
 * in a collapsible, resizable panel (~380px to 440px).
 */

import React, { useState } from "react";
import {
  Terminal,
  FileCode,
  ShieldCheck,
  PanelRightClose,
  Maximize2,
  Minimize2,
  Radio,
} from "lucide-react";
import { DrawerTab, EvidenceView, RunDetail } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";
import { TelemetryTerminal } from "../terminal/TelemetryTerminal";
import { UnifiedDiffViewer } from "../diff/UnifiedDiffViewer";
import { EvidenceLedger } from "../evidence/EvidenceLedger";
import { client } from "../../api/client";

export interface RightDrawerProps {
  run?: RunDetail | null;
  evidence?: EvidenceView | null;
  activeTab?: DrawerTab;
  onTabChange?: (tab: DrawerTab) => void;
  width?: number;
  onWidthChange?: (width: number) => void;
  onClose?: () => void;
}

export const RightDrawer: React.FC<RightDrawerProps> = ({
  run: propRun,
  evidence: propEvidence,
  activeTab: propActiveTab,
  onTabChange,
  width: propWidth,
  onWidthChange,
  onClose,
}) => {
  const store = useEngineStore();
  const run = propRun !== undefined ? propRun : store.run;
  const evidence = propEvidence !== undefined ? propEvidence : store.evidence;
  const currentTab = propActiveTab ?? store.selectedTab ?? "terminal";

  // Width management (380px - 440px)
  const [internalWidth, setInternalWidth] = useState<number>(400);
  const activeWidth = propWidth ?? internalWidth;

  const setDrawerWidth = (newWidth: number) => {
    const clamped = Math.min(440, Math.max(380, newWidth));
    if (onWidthChange) {
      onWidthChange(clamped);
    } else {
      setInternalWidth(clamped);
    }
  };

  const handleTabSelect = (tab: DrawerTab) => {
    if (onTabChange) {
      onTabChange(tab);
    } else {
      store.setSelectedTab(tab);
    }
  };

  const handleClose = () => {
    if (onClose) {
      onClose();
    } else {
      store.setRightDrawerOpen(false);
    }
  };

  // Determine changed files count for diff tab badge
  let diffCount =
    run?.gate?.candidate?.changed_files?.length ||
    (run as any)?.changed_files?.length ||
    0;

  if (diffCount === 0 && evidence?.latest_diff) {
    const matches = evidence.latest_diff.match(/diff --git /g);
    if (matches) {
      diffCount = matches.length;
    }
  }

  const token = client.getToken() || "none";

  const tabs: { id: DrawerTab; label: string; icon: React.ReactNode; testId: string }[] = [
    {
      id: "terminal",
      label: "Terminal & Logi",
      icon: <Terminal className="w-3.5 h-3.5 shrink-0" />,
      testId: "drawer-tab-terminal",
    },
    {
      id: "diff",
      label: diffCount > 0 ? `Unified Diff (${diffCount})` : "Unified Diff",
      icon: <FileCode className="w-3.5 h-3.5 shrink-0" />,
      testId: "drawer-tab-diff",
    },
    {
      id: "evidence",
      label: "Księga Dowodów",
      icon: <ShieldCheck className="w-3.5 h-3.5 shrink-0" />,
      testId: "drawer-tab-evidence",
    },
  ];

  return (
    <div
      data-testid="right-drawer-root"
      style={propWidth ? { width: `${activeWidth}px` } : undefined}
      className="flex flex-col h-full w-full bg-surface overflow-hidden select-none"
    >
      {/* Drawer Top Header */}
      <div
        data-testid="right-drawer-header"
        className="p-3 border-b border-subtle bg-elevated/40 flex flex-col gap-2 shrink-0"
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-sky-400 animate-pulse" />
            <h2 className="text-xs font-bold uppercase tracking-wider text-white">
              Panel Telemetrii & Dowodów
            </h2>
          </div>

          {/* Width adjustment & Close action */}
          <div className="flex items-center gap-1">
            <button
              onClick={() => setDrawerWidth(activeWidth === 440 ? 380 : 440)}
              data-testid="toggle-drawer-width"
              title={activeWidth === 440 ? "Zwęź do 380px" : "Rozszerz do 440px"}
              className="p-1 rounded text-slate-400 hover:text-slate-200 hover:bg-elevated transition"
            >
              {activeWidth === 440 ? (
                <Minimize2 className="w-3.5 h-3.5" />
              ) : (
                <Maximize2 className="w-3.5 h-3.5" />
              )}
            </button>

            <button
              onClick={handleClose}
              data-testid="close-right-drawer"
              title="Zwiń panel boczny"
              className="p-1 rounded text-slate-400 hover:text-rose-300 hover:bg-rose-500/10 transition"
            >
              <PanelRightClose className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        {/* Tab Navigation Switcher */}
        <div
          data-testid="drawer-tab-navigation"
          className="flex gap-1 bg-surface p-1 rounded-lg border border-subtle"
        >
          {tabs.map((tab) => {
            const isActive = currentTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => handleTabSelect(tab.id)}
                data-testid={tab.testId}
                className={`flex-1 py-1.5 px-2 text-xs font-mono font-medium rounded transition flex items-center justify-center gap-1.5 ${
                  isActive
                    ? "bg-phase-execute text-sky-950 font-bold shadow-sm"
                    : "text-slate-400 hover:text-slate-200 hover:bg-elevated/50"
                }`}
              >
                {tab.icon}
                <span className="truncate">{tab.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Main Tab Viewport */}
      <div className="flex-1 overflow-hidden flex flex-col">
        {currentTab === "terminal" && (
          <TelemetryTerminal run={run} evidence={evidence} />
        )}

        {currentTab === "diff" && (
          <UnifiedDiffViewer run={run} evidence={evidence} />
        )}

        {currentTab === "evidence" && (
          <EvidenceLedger run={run} evidence={evidence} />
        )}
      </div>

      {/* Drawer Status Footer */}
      <div
        data-testid="right-drawer-footer"
        className="p-2.5 border-t border-subtle bg-elevated/40 text-[10px] font-mono text-slate-400 flex items-center justify-between shrink-0"
      >
        <div className="flex items-center gap-1.5 truncate max-w-[200px]">
          <span className="text-slate-500">Token:</span>
          <span className="truncate text-slate-300">
            {token !== "none" ? `${token.substring(0, 10)}...` : "brak"}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-emerald-400 flex items-center gap-1">
            <Radio className="w-3 h-3 text-emerald-400 animate-pulse" />
            LOOPBACK OK
          </span>
        </div>
      </div>
    </div>
  );
};
