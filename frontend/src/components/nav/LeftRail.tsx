/**
 * AAW Agentic Studio — Left Navigation Rail
 *
 * Hosts navigation items, AI Provider Health cards (Claude, Codex, Antigravity),
 * Autonomy Policy presets, and the Universal MCP Card.
 */

import React from "react";
import { BarChart3,
  Activity,
  PlusCircle,
  History,
  Settings,
  Zap,
  Sparkles,
  Bot,
  Cpu,
  SlidersHorizontal,
} from "lucide-react";
import { Provider } from "../../api/types";
import { useEngineStore, NavItem, AutonomyPreset } from "../../store/engineStore";

export interface LeftRailProps {
  className?: string;
  onOpenMCPModal?: () => void;
}

export const LeftRail: React.FC<LeftRailProps> = ({
  className = "",
  onOpenMCPModal,
}) => {
  const {
    bootstrap,
    mcpStatus,
    activeNav,
    activePolicyPreset,
    setActiveNav,
    setActivePolicyPreset,
    setMCPModalOpen,
  } = useEngineStore();

  const handleOpenMCP = onOpenMCPModal || (() => setMCPModalOpen(true));

  // Navigation Items
  const navItems: { id: NavItem; label: string; icon: React.ElementType }[] = [
    { id: "command-center", label: "Live Command Center", icon: Activity },
    { id: "new-task", label: "New Task Wizard", icon: PlusCircle },
    { id: "runs", label: "Task Runs", icon: History },
    { id: "experience", label: "Experience & Benchmark", icon: BarChart3 },
    { id: "settings", label: "Settings", icon: Settings },
  ];

  // Extract Provider Health info
  const providersList: Provider[] = bootstrap?.providers?.providers || [];
  const claudeProvider = providersList.find((p) => p.provider_id === "claude");
  const codexProvider = providersList.find((p) => p.provider_id === "codex");
  const agyProvider = providersList.find((p) => p.provider_id === "antigravity");

  const getProviderStatusBadge = (provider?: Provider) => {
    if (!provider || provider.status === "NOT_FOUND") {
      return {
        badge: "NOT FOUND" as const,
        badgeClass: "bg-slate-800 text-slate-400 border-slate-700",
        isReady: false,
        version: null,
      };
    }
    if (
      provider.login === "LOGGED_IN" ||
      (provider.runnable_profiles && provider.runnable_profiles.length > 0)
    ) {
      return {
        badge: "READY" as const,
        badgeClass: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
        isReady: true,
        version: provider.version,
      };
    }
    return {
      badge: "NOT LOGGED IN" as const,
      badgeClass: "bg-amber-500/10 text-amber-400 border-amber-500/30",
      isReady: false,
      version: provider.version,
    };
  };

  const claudeStatus = getProviderStatusBadge(claudeProvider);
  const codexStatus = getProviderStatusBadge(codexProvider);
  const agyStatus = getProviderStatusBadge(agyProvider);

  const readyCount = [claudeStatus.isReady, codexStatus.isReady, agyStatus.isReady].filter(
    Boolean
  ).length;

  // Autonomy Policy Presets
  const policyPresets: {
    id: AutonomyPreset;
    name: string;
    description: string;
    badge: string;
  }[] = [
    {
      id: "conservative",
      name: "Konserwatywny",
      description: "Częste bramki decyzyjne i ścisła weryfikacja",
      badge: "Wysokie bezpieczeństwo",
    },
    {
      id: "balanced",
      name: "Zbalansowany",
      description: "Automatyczny cykl, pauza na bramkach krytycznych",
      badge: "Zalecany",
    },
    {
      id: "autonomous",
      name: "Pełna autonomia",
      description: "Ciągła praca do wyczerpania roadmapy",
      badge: "Długa praca",
    },
  ];

  // MCP status calculations
  const configuredClientsCount = [
    Boolean(mcpStatus?.antigravity?.configured),
    Boolean(mcpStatus?.claude?.configured),
    Boolean(mcpStatus?.codex?.configured),
  ].filter(Boolean).length;

  const isMcpReady = configuredClientsCount > 0;

  return (
    <aside
      className={`w-[270px] bg-surface flex flex-col justify-between p-3.5 select-none text-slate-200 ${className}`}
      data-testid="left-rail"
    >
      <div className="space-y-5">
        {/* Navigation Section */}
        <div>
          <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 px-2.5 mb-1.5">
            Nawigacja
          </div>
          <nav className="space-y-1">
            {navItems.map((item) => {
              const Icon = item.icon;
              const isActive = activeNav === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => setActiveNav(item.id)}
                  data-testid={`nav-item-${item.id}`}
                  className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium transition group ${
                    isActive
                      ? "bg-elevated text-sky-400 font-semibold border-l-2 border-sky-400 shadow-sm"
                      : "text-slate-400 hover:text-slate-200 hover:bg-elevated/50"
                  }`}
                >
                  <Icon
                    className={`w-4 h-4 transition ${
                      isActive ? "text-sky-400" : "text-slate-400 group-hover:text-slate-200"
                    }`}
                  />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </nav>
        </div>

        {/* AI Providers Health Card */}
        <div className="p-3 bg-elevated/40 border border-subtle rounded-xl" data-testid="ai-providers-card">
          <div className="flex items-center justify-between mb-2.5 pb-1.5 border-b border-subtle/60">
            <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-200">
              <Cpu className="w-3.5 h-3.5 text-sky-400" />
              <span>Dostawcy AI</span>
            </div>
            <span
              className={`text-[10px] font-mono font-bold px-1.5 py-0.5 rounded ${
                readyCount > 0
                  ? "bg-emerald-500/10 text-emerald-400"
                  : "bg-slate-800 text-slate-400"
              }`}
            >
              {readyCount}/3 GOTOWYCH
            </span>
          </div>

          <div className="space-y-2">
            {/* Claude */}
            <div className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-2">
                <Bot className="w-3.5 h-3.5 text-slate-400" />
                <span className="text-slate-200 font-medium">Claude</span>
              </div>
              <span
                data-testid="provider-badge-claude"
                className={`px-1.5 py-0.5 text-[9px] font-mono font-bold rounded border ${claudeStatus.badgeClass}`}
              >
                {claudeStatus.badge}
              </span>
            </div>

            {/* Codex */}
            <div className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-2">
                <Cpu className="w-3.5 h-3.5 text-slate-400" />
                <span className="text-slate-200 font-medium">Codex</span>
              </div>
              <span
                data-testid="provider-badge-codex"
                className={`px-1.5 py-0.5 text-[9px] font-mono font-bold rounded border ${codexStatus.badgeClass}`}
              >
                {codexStatus.badge}
              </span>
            </div>

            {/* Antigravity */}
            <div className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-2">
                <Sparkles className="w-3.5 h-3.5 text-slate-400" />
                <span className="text-slate-200 font-medium">Antigravity</span>
              </div>
              <span
                data-testid="provider-badge-antigravity"
                className={`px-1.5 py-0.5 text-[9px] font-mono font-bold rounded border ${agyStatus.badgeClass}`}
              >
                {agyStatus.badge}
              </span>
            </div>
          </div>
        </div>

        {/* Autonomy Policy Presets */}
        <div data-testid="autonomy-presets-section">
          <div className="flex items-center justify-between px-2.5 mb-1.5">
            <span className="text-[10px] uppercase font-bold tracking-wider text-slate-400">
              Polityka autonomii
            </span>
            <SlidersHorizontal className="w-3 h-3 text-slate-500" />
          </div>

          <div className="space-y-1.5">
            {policyPresets.map((preset) => {
              const isSelected = activePolicyPreset === preset.id;
              return (
                <button
                  key={preset.id}
                  onClick={() => setActivePolicyPreset(preset.id)}
                  data-testid={`preset-btn-${preset.id}`}
                  className={`w-full text-left p-2.5 rounded-lg border transition ${
                    isSelected
                      ? "bg-phase-execute/10 border-phase-execute/40 shadow-sm"
                      : "bg-surface hover:bg-elevated/40 border-subtle"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span
                      className={`text-xs font-semibold ${
                        isSelected ? "text-sky-300 font-bold" : "text-slate-200"
                      }`}
                    >
                      {preset.name}
                    </span>
                    <span
                      className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${
                        isSelected
                          ? "bg-sky-500/20 text-sky-300"
                          : "bg-slate-800 text-slate-500"
                      }`}
                    >
                      {preset.badge}
                    </span>
                  </div>
                  <p className="text-[10px] text-slate-400 mt-1 leading-tight line-clamp-1">
                    {preset.description}
                  </p>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Universal MCP Card */}
      <div className="pt-4 border-t border-subtle mt-4">
        <div
          className="p-3 bg-gradient-to-br from-elevated/80 to-surface border border-subtle rounded-xl shadow-lg"
          data-testid="universal-mcp-card"
        >
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-1.5">
              <Zap className="w-4 h-4 text-sky-400" />
              <span className="text-xs font-bold text-slate-100">Universal MCP</span>
            </div>
            <span
              className={`text-[9px] font-mono font-bold px-1.5 py-0.5 rounded-full border ${
                isMcpReady
                  ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                  : "bg-amber-500/10 text-amber-400 border-amber-500/30"
              }`}
            >
              {isMcpReady ? `${configuredClientsCount}/3 AKTYWNE` : "NIEAKTYWNE"}
            </span>
          </div>

          <p className="text-[11px] text-slate-400 mb-2.5 font-mono">
            8 wbudowanych narzędzi sterowania
          </p>

          <button
            onClick={handleOpenMCP}
            data-testid="mcp-configure-button"
            className="w-full py-1.5 px-2.5 text-xs font-bold text-sky-950 bg-sky-400 hover:bg-sky-300 rounded-lg shadow-sm flex items-center justify-center gap-1.5 transition active:scale-[0.98]"
          >
            <Zap className="w-3.5 h-3.5 fill-sky-950" />
            <span>⚡ Konfiguruj (1-Click)</span>
          </button>
        </div>
      </div>
    </aside>
  );
};
