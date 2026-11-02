/**
 * AAW Agentic Studio — Settings View
 *
 * Configuration view for AAW engine behavior, model defaults,
 * provider timeouts, and Universal MCP integrations.
 */

import React, { useState, useEffect } from "react";
import {
  Settings,
  Shield,
  Bot,
  Save,
  CheckCircle2,
  AlertCircle,
  Network,
  RotateCw,
  ExternalLink,
} from "lucide-react";
import { client } from "../../api/client";
import { useEngineStore } from "../../store/engineStore";

export const SettingsView: React.FC = () => {
  const {
    mcpStatus,
    setMCPModalOpen,
  } = useEngineStore();

  const [settings, setSettings] = useState<Record<string, any>>({
    planning: "STRONGEST",
    implementation: "RECOMMENDED",
    review: "RECOMMENDED",
    online_recommendation_updates: false,
    max_repair_attempts: 2,
    provider_timeout_s: 3600,
  });

  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [saveSuccess, setSaveSuccess] = useState<boolean>(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchSettings() {
      setLoading(true);
      try {
        const data = await client.getSettings();
        if (data) {
          setSettings((prev) => ({ ...prev, ...data }));
        }
      } catch (err: any) {
        setSaveError(err?.message || "Nie udało się załadować ustawień");
      } finally {
        setLoading(false);
      }
    }
    fetchSettings();
  }, []);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveSuccess(false);
    setSaveError(null);

    const payload = {
      ...settings,
      max_repair_attempts: Number(settings.max_repair_attempts),
      provider_timeout_s: Number(settings.provider_timeout_s),
      online_recommendation_updates: Boolean(settings.online_recommendation_updates),
    };

    try {
      const updated = await client.saveSettings(payload);
      if (updated) {
        setSettings(updated);
      }
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 4000);
    } catch (err: any) {
      setSaveError(err?.message || "Błąd zapisu ustawień");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-4xl w-full mx-auto space-y-6 animate-fade-in pb-12" data-testid="settings-view">
      {/* Header */}
      <div className="bg-surface border border-subtle rounded-xl p-5 shadow-xl flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs font-mono text-sky-400 uppercase mb-1">
            <Settings className="w-3.5 h-3.5" />
            <span>Konfiguracja Silnika • Parametry Bezpieczeństwa</span>
          </div>
          <h2 className="text-xl font-bold tracking-tight text-white">
            Ustawienia Systemowe AAW
          </h2>
          <p className="text-xs text-slate-400 mt-1 max-w-2xl">
            Dostosuj zachowanie pętli autonomicznej, limity napraw, domyślne profile modeli AI oraz integrację z Universal Model Context Protocol (MCP).
          </p>
        </div>
      </div>

      {saveSuccess && (
        <div className="p-4 bg-emerald-500/10 border border-emerald-500/30 rounded-xl flex items-center gap-3 text-emerald-300 text-xs shadow-lg animate-fade-in">
          <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
          <span>Ustawienia zostały pomyślnie zapisane w settings.json.</span>
        </div>
      )}

      {saveError && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-xl flex items-center gap-3 text-rose-300 text-xs shadow-lg">
          <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
          <span>{saveError}</span>
        </div>
      )}

      <form onSubmit={handleSave} className="space-y-6">
        {/* Section 1: Loop Limits & Safety */}
        <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <Shield className="w-4 h-4 text-sky-400" />
              <span>1. Bezpieczniki Pętli Autonomicznej i Limity Czasowe</span>
            </div>
            <span className="text-[11px] font-mono text-slate-400">Drabina eskalacji i progi</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 flex items-center justify-between">
                <span>Maksymalna liczba prób naprawy (REPAIR loop):</span>
                <span className="font-mono text-sky-400 text-xs">{settings.max_repair_attempts}</span>
              </label>
              <input
                type="number"
                min={1}
                max={6}
                value={settings.max_repair_attempts}
                onChange={(e) => setSettings({ ...settings, max_repair_attempts: e.target.value })}
                className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none focus:border-sky-500"
              />
              <p className="text-[11px] text-slate-500">
                Po tylu nieudanych próbach naprawy silnik automatycznie eskaluje do bramki Human Gate (błąd E_REPAIR_LIMIT).
              </p>
            </div>

            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 flex items-center justify-between">
                <span>Limit czasu na wywołanie dostawcy CLI (sekundy):</span>
                <span className="font-mono text-sky-400 text-xs">{settings.provider_timeout_s}s</span>
              </label>
              <input
                type="number"
                min={60}
                max={14400}
                step={60}
                value={settings.provider_timeout_s}
                onChange={(e) => setSettings({ ...settings, provider_timeout_s: e.target.value })}
                className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none focus:border-sky-500"
              />
              <p className="text-[11px] text-slate-500">
                Domyślnie 3600s (1h). Zapobiega zawieszeniu zadania w przypadku braku odpowiedzi od API/CLI.
              </p>
            </div>
          </div>
        </div>

        {/* Section 2: Model Defaults */}
        <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <Bot className="w-4 h-4 text-sky-400" />
              <span>2. Domyślne Tryby Przydziału Modeli</span>
            </div>
            <span className="text-[11px] font-mono text-slate-400">Strategie routingu</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 block">
                Planowanie (Planning Mode):
              </label>
              <select
                value={settings.planning || "STRONGEST"}
                onChange={(e) => setSettings({ ...settings, planning: e.target.value })}
                className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500"
              >
                <option value="STRONGEST">STRONGEST (Najmocniejszy model)</option>
                <option value="RECOMMENDED">RECOMMENDED (Rekomendowany)</option>
                <option value="BALANCED">BALANCED (Zrównoważony koszt)</option>
              </select>
            </div>

            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 block">
                Implementacja (Implementation Mode):
              </label>
              <select
                value={settings.implementation || "RECOMMENDED"}
                onChange={(e) => setSettings({ ...settings, implementation: e.target.value })}
                className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500"
              >
                <option value="RECOMMENDED">RECOMMENDED (Rekomendowany)</option>
                <option value="STRONGEST">STRONGEST (Maksymalna siła)</option>
                <option value="FAST">FAST (Ekonomiczny / Gemini Flash)</option>
              </select>
            </div>

            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 block">
                Review (Review Mode):
              </label>
              <select
                value={settings.review || "RECOMMENDED"}
                onChange={(e) => setSettings({ ...settings, review: e.target.value })}
                className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500"
              >
                <option value="RECOMMENDED">RECOMMENDED (Niezależny model)</option>
                <option value="STRONGEST">STRONGEST (Rygorystyczne review)</option>
              </select>
            </div>
          </div>
        </div>

        {/* Section 3: Universal MCP Hub */}
        <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <Network className="w-4 h-4 text-sky-400" />
              <span>3. Universal Model Context Protocol (MCP) Hub</span>
            </div>
            <span className="text-[11px] font-mono text-emerald-400">Endpoint: /mcp</span>
          </div>

          <p className="text-xs text-slate-300 leading-relaxed">
            Serwer AAW udostępnia standardowy protokół MCP na porcie loopback z weryfikacją tokenu i nagłówka Host. Dowolny zewnętrzny klient (Antigravity IDE, Claude Desktop, Cursor, Codex CLI) może sterować silnikiem jako narzędziem agentowym.
          </p>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-1">
            <div className="p-3 bg-elevated/40 border border-subtle rounded-lg">
              <div className="text-[11px] font-bold text-slate-300 mb-1">Google Antigravity</div>
              <div className="text-xs font-mono text-emerald-400 flex items-center gap-1.5">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>{mcpStatus?.antigravity?.configured ? "Skonfigurowany" : "Wykryty"}</span>
              </div>
            </div>

            <div className="p-3 bg-elevated/40 border border-subtle rounded-lg">
              <div className="text-[11px] font-bold text-slate-300 mb-1">Claude Desktop</div>
              <div className="text-xs font-mono text-emerald-400 flex items-center gap-1.5">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>{mcpStatus?.claude?.configured ? "Skonfigurowany" : "Wykryty"}</span>
              </div>
            </div>

            <div className="p-3 bg-elevated/40 border border-subtle rounded-lg">
              <div className="text-[11px] font-bold text-slate-300 mb-1">Codex CLI</div>
              <div className="text-xs font-mono text-emerald-400 flex items-center gap-1.5">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>{mcpStatus?.codex?.configured ? "Skonfigurowany" : "Wykryty"}</span>
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={() => setMCPModalOpen(true)}
            className="px-3 py-1.5 bg-elevated hover:bg-slate-700 text-xs font-medium text-sky-400 border border-subtle rounded-lg flex items-center gap-1.5 transition"
          >
            <ExternalLink className="w-3.5 h-3.5" />
            <span>Otwórz Kreator Konfiguracji MCP (1-Click Install)</span>
          </button>
        </div>

        {/* Submit */}
        <div className="flex items-center justify-end pt-2">
          <button
            type="submit"
            disabled={saving || loading}
            className="px-6 py-2.5 bg-sky-600 hover:bg-sky-500 text-xs font-bold text-white rounded-lg flex items-center gap-2 shadow-lg transition disabled:opacity-50"
            data-testid="save-settings-btn"
          >
            {saving ? (
              <>
                <RotateCw className="w-4 h-4 animate-spin" />
                <span>Zapisywanie parametrów...</span>
              </>
            ) : (
              <>
                <Save className="w-4 h-4" />
                <span>Zapisz Ustawienia</span>
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
};
