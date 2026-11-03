/**
 * AAW Agentic Studio — Adaptation Panel (roadmap 1.2 / 2.4 / 4.4)
 *
 * An honest status panel for AAW's adaptive features. Mode names, the verified-runs
 * threshold and the refusal codes are copied from adaptive_policy.py. Nothing in this
 * panel unlocks a mode; the backend decides that.
 */

import React, { useEffect, useState } from "react";
import { Activity, Gauge, Lock, Sparkles, RotateCw, CheckCircle2, AlertCircle, Info } from "lucide-react";
import { client } from "../../api/client";
import { useEngineStore } from "../../store/engineStore";

// adaptive_policy.py: MODE_SHADOW, MODE_CONTROLLED_ADAPTIVE, MODE_ACTIVE
const MODE_SHADOW = "SHADOW";
const MODE_CONTROLLED_ADAPTIVE = "CONTROLLED_ADAPTIVE";
const MODE_ACTIVE = "ACTIVE";
// adaptive_policy.py: MIN_VERIFIED_RUNS_FOR_CONTROLLED
const MIN_VERIFIED_RUNS_FOR_CONTROLLED = 100;
// adaptive_policy.py: REFUSAL_CONTROLLED_LOCKED (code part before ":") and the ACTIVE refusal reason
const REFUSAL_CONTROLLED_CODE = "CONTROLLED_ADAPTIVE_LOCKED_PENDING_TRUSTED_AUTHORIZATION";
const REFUSAL_ACTIVE_CODE = "UNAUTHORIZED_ACTIVE_MODE";

// Shape of run.adaptive_orchestration (product_view.py: _adaptive_orchestration_data).
interface AdaptiveOrchestrationData {
  enabled?: boolean;
  selected_template?: string | null;
  max_parallel_workers?: number | null;
  active_workers_count?: number | null;
  serialization_reason?: string | null;
  strategy?: {
    workers_count?: number | null;
    reason_code?: string | null;
    [key: string]: any;
  } | null;
}

const PLANNED_ITEMS: string[] = [
  "Podpisana autoryzacja trybu kontrolowanego (etap 4.1)",
  "Canary: maks. 2 agenty, tylko zadania o złożoności do LOW (etap 4.2)",
  "Circuit breaker przełączający do DISABLED po awarii (etap 4.2)",
  "Równoległe kroki work packetu przez workflow DAG (etap 4.3)",
];

function valueOrDash(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export const AdaptationPanel: React.FC = () => {
  const { run } = useEngineStore();

  const adaptive: AdaptiveOrchestrationData | null =
    ((run as unknown) as { adaptive_orchestration?: AdaptiveOrchestrationData | null } | null)
      ?.adaptive_orchestration ?? null;

  // ── Controlled exploration settings (opt-in) ─────────────────────────────
  const [explorationEnabled, setExplorationEnabled] = useState<boolean>(false);
  const [maxPercent, setMaxPercent] = useState<string>("20");
  const [maxPerRun, setMaxPerRun] = useState<string>("3");
  const [settingsLoaded, setSettingsLoaded] = useState<boolean>(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState<boolean>(false);
  const [saveOk, setSaveOk] = useState<boolean>(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    client
      .getSettings()
      .then((data) => {
        if (!active || !data) return;
        if (typeof data.exploration_enabled === "boolean") {
          setExplorationEnabled(data.exploration_enabled);
        }
        if (data.exploration_max_percent !== undefined && data.exploration_max_percent !== null) {
          setMaxPercent(String(data.exploration_max_percent));
        }
        if (data.exploration_max_per_run !== undefined && data.exploration_max_per_run !== null) {
          setMaxPerRun(String(data.exploration_max_per_run));
        }
        setSettingsLoaded(true);
      })
      .catch((err: any) => {
        if (active) {
          setLoadError(err?.message || "Nie udało się załadować ustawień eksploracji");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  // Backend validation (product_home.save_settings): percent 1-50, per run 1-10.
  const percentNum = Number(maxPercent);
  const perRunNum = Number(maxPerRun);
  const percentValid = Number.isInteger(percentNum) && percentNum >= 1 && percentNum <= 50;
  const perRunValid = Number.isInteger(perRunNum) && perRunNum >= 1 && perRunNum <= 10;
  const canSave = settingsLoaded && percentValid && perRunValid && !saving;

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setSaveOk(false);
    setSaveError(null);
    try {
      await client.saveSettings({
        exploration_enabled: explorationEnabled,
        exploration_max_percent: percentNum,
        exploration_max_per_run: perRunNum,
      });
      setSaveOk(true);
    } catch (err: any) {
      setSaveError(err?.message || "Nie udało się zapisać ustawień eksploracji");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="adaptation-panel">
      {/* Section: Tryb adaptacyjny */}
      <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4" data-testid="adaptation-mode-section">
        <div className="flex items-center justify-between border-b border-subtle pb-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <Gauge className="w-4 h-4 text-sky-400" />
            <span>Tryb adaptacyjny</span>
          </div>
          <span className="text-[11px] font-mono text-slate-400">Tylko do odczytu</span>
        </div>

        <div className="space-y-2">
          <div
            className="p-3 bg-elevated/40 border border-subtle rounded-lg flex flex-wrap items-center justify-between gap-2"
            data-testid="mode-shadow"
          >
            <div className="space-y-0.5">
              <div className="text-xs font-bold text-slate-200 font-mono">{MODE_SHADOW}</div>
              <p className="text-[11px] text-slate-400">
                Tryb pasywny: liczy rekomendację i zapisuje ją przy przebiegu. Nigdy nie zmienia wykonania.
              </p>
            </div>
            <span className="text-[11px] font-mono text-emerald-400 flex items-center gap-1">
              <CheckCircle2 className="w-3.5 h-3.5" />
              <span>Aktywny (domyślny)</span>
            </span>
          </div>

          <div
            className="p-3 bg-elevated/40 border border-subtle rounded-lg flex flex-wrap items-center justify-between gap-2"
            data-testid="mode-controlled-adaptive"
          >
            <div className="space-y-0.5">
              <div className="text-xs font-bold text-slate-200 font-mono">{MODE_CONTROLLED_ADAPTIVE}</div>
              <p className="text-[11px] text-slate-400">
                {`Wymaga podpisanej autoryzacji operatora oraz minimum ${MIN_VERIFIED_RUNS_FOR_CONTROLLED} zweryfikowanych przebiegów produkcyjnych (etap 4 roadmapy).`}
              </p>
              <p className="text-[11px] font-mono text-slate-500 break-all" data-testid="refusal-controlled-code">
                {REFUSAL_CONTROLLED_CODE}
              </p>
            </div>
            <span className="text-[11px] font-mono text-amber-400 flex items-center gap-1">
              <Lock className="w-3.5 h-3.5" />
              <span>Zablokowany</span>
            </span>
          </div>

          <div
            className="p-3 bg-elevated/40 border border-subtle rounded-lg flex flex-wrap items-center justify-between gap-2"
            data-testid="mode-active"
          >
            <div className="space-y-0.5">
              <div className="text-xs font-bold text-slate-200 font-mono">{MODE_ACTIVE}</div>
              <p className="text-[11px] text-slate-400">Tryb trwale zablokowany.</p>
              <p className="text-[11px] font-mono text-slate-500 break-all" data-testid="refusal-active-code">
                {REFUSAL_ACTIVE_CODE}
              </p>
            </div>
            <span className="text-[11px] font-mono text-rose-400 flex items-center gap-1">
              <Lock className="w-3.5 h-3.5" />
              <span>Zablokowany na stałe</span>
            </span>
          </div>
        </div>

        <div
          className="p-3 bg-elevated/40 border border-subtle rounded-lg flex flex-wrap items-center justify-between gap-2 text-xs"
          data-testid="verified-runs-progress"
        >
          <span className="text-slate-300">Zweryfikowane przebiegi produkcyjne:</span>
          <span className="font-mono text-slate-400">licznik pojawi się po wdrożeniu etapu 1.1</span>
        </div>
        <p className="text-[11px] text-slate-500">
          {`Próg odblokowania trybu kontrolowanego: ${MIN_VERIFIED_RUNS_FOR_CONTROLLED} zweryfikowanych przebiegów.`}
        </p>

        {/* Shadow recommendation for the currently loaded run */}
        <div className="space-y-2" data-testid="shadow-recommendation">
          <div className="text-xs font-semibold text-slate-200 flex items-center gap-2">
            <Sparkles className="w-3.5 h-3.5 text-sky-400" />
            <span>Rekomendacja SHADOW dla bieżącego przebiegu</span>
          </div>

          {!run ? (
            <p className="text-[11px] text-slate-500" data-testid="shadow-no-run">
              Otwórz przebieg, aby zobaczyć, co dla niego polecał tryb SHADOW.
            </p>
          ) : !adaptive ? (
            <p className="text-[11px] text-slate-500" data-testid="shadow-no-data">
              {`Brak danych adaptacyjnych dla przebiegu ${run.run_id}.`}
            </p>
          ) : (
            <div className="space-y-2">
              <p className="text-[11px] text-slate-400 flex items-center gap-1.5" data-testid="shadow-enabled-note">
                {adaptive.enabled ? (
                  <>
                    <Activity className="w-3.5 h-3.5 text-sky-400 shrink-0" />
                    <span>Rekomendacja SHADOW (pasywna, nie zmieniła wykonania).</span>
                  </>
                ) : (
                  <>
                    <Info className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                    <span>Adaptacja nie była włączona dla tego przebiegu. Poniżej tylko zapisane dane.</span>
                  </>
                )}
              </p>
              <dl className="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
                <div className="p-2 bg-elevated/40 border border-subtle rounded-lg">
                  <dt className="text-[11px] text-slate-500">Wybrany szablon</dt>
                  <dd className="font-mono text-slate-200 break-all" data-testid="shadow-template">
                    {valueOrDash(adaptive.selected_template)}
                  </dd>
                </div>
                <div className="p-2 bg-elevated/40 border border-subtle rounded-lg">
                  <dt className="text-[11px] text-slate-500">Workery (maks. równolegle)</dt>
                  <dd className="font-mono text-slate-200" data-testid="shadow-workers">
                    {valueOrDash(adaptive.max_parallel_workers ?? adaptive.strategy?.workers_count)}
                  </dd>
                </div>
                <div className="p-2 bg-elevated/40 border border-subtle rounded-lg">
                  <dt className="text-[11px] text-slate-500">Powód szeregowania</dt>
                  <dd className="font-mono text-slate-200 break-all" data-testid="shadow-serialization">
                    {valueOrDash(adaptive.serialization_reason)}
                  </dd>
                </div>
              </dl>
            </div>
          )}
        </div>
      </div>

      {/* Section: Kontrolowana eksploracja */}
      <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4" data-testid="exploration-section">
        <div className="flex items-center justify-between border-b border-subtle pb-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <Sparkles className="w-4 h-4 text-sky-400" />
            <span>Kontrolowana eksploracja</span>
          </div>
          <span className="text-[11px] font-mono text-slate-400">Opt-in</span>
        </div>

        <p className="text-xs text-slate-300 leading-relaxed">
          Mała, widoczna część zwykłych iteracji trafia do innego, już dostępnego modelu tej samej lub niższej klasy
          kosztu, dzięki czemu benchmark wypełnia się z prawdziwej pracy. Eksploracja nigdy nie naprawia błędów i nie
          obejmuje krytycznego zakresu.
        </p>

        {loadError && (
          <div
            className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-lg flex items-start gap-2 text-rose-300 text-xs"
            data-testid="exploration-load-error"
          >
            <AlertCircle className="w-4 h-4 text-rose-400 shrink-0 mt-px" />
            <span className="break-words">{loadError}</span>
          </div>
        )}

        <div className="flex items-center justify-between gap-4 p-3 bg-elevated/40 border border-subtle rounded-lg">
          <div className="space-y-0.5">
            <div className="text-xs font-medium text-slate-200">Włącz kontrolowaną eksplorację</div>
            <div className="text-[11px] font-mono text-slate-500">exploration_enabled</div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={explorationEnabled}
            aria-label="Włącz kontrolowaną eksplorację"
            disabled={!settingsLoaded || saving}
            onClick={() => setExplorationEnabled((v) => !v)}
            data-testid="exploration-toggle"
            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-subtle transition disabled:opacity-50 ${
              explorationEnabled ? "bg-sky-600" : "bg-elevated"
            }`}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white transition ${
                explorationEnabled ? "translate-x-6" : "translate-x-1"
              }`}
            />
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-300 block" htmlFor="exploration-max-percent">
              Maks. udział iteracji (%):
            </label>
            <input
              id="exploration-max-percent"
              type="number"
              min={1}
              max={50}
              step={1}
              value={maxPercent}
              disabled={!settingsLoaded || saving}
              onChange={(e) => setMaxPercent(e.target.value)}
              data-testid="exploration-max-percent"
              className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none focus:border-sky-500 disabled:opacity-50"
            />
            <p className="text-[11px] text-slate-500">Zakres: 1–50.</p>
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-300 block" htmlFor="exploration-max-per-run">
              Maks. iteracji na przebieg:
            </label>
            <input
              id="exploration-max-per-run"
              type="number"
              min={1}
              max={10}
              step={1}
              value={maxPerRun}
              disabled={!settingsLoaded || saving}
              onChange={(e) => setMaxPerRun(e.target.value)}
              data-testid="exploration-max-per-run"
              className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none focus:border-sky-500 disabled:opacity-50"
            />
            <p className="text-[11px] text-slate-500">Zakres: 1–10.</p>
          </div>
        </div>

        {!percentValid && settingsLoaded && (
          <p className="text-[11px] text-rose-300" data-testid="exploration-percent-invalid">
            Udział musi być liczbą całkowitą od 1 do 50.
          </p>
        )}
        {!perRunValid && settingsLoaded && (
          <p className="text-[11px] text-rose-300" data-testid="exploration-per-run-invalid">
            Liczba iteracji na przebieg musi być liczbą całkowitą od 1 do 10.
          </p>
        )}

        {saveOk && (
          <p className="text-[11px] text-emerald-300 flex items-center gap-1.5" data-testid="exploration-save-ok">
            <CheckCircle2 className="w-3.5 h-3.5" />
            <span>Ustawienia eksploracji zapisane.</span>
          </p>
        )}
        {saveError && (
          <div
            className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-lg flex items-start gap-2 text-rose-300 text-xs"
            data-testid="exploration-save-error"
          >
            <AlertCircle className="w-4 h-4 text-rose-400 shrink-0 mt-px" />
            <span className="break-words">{saveError}</span>
          </div>
        )}

        <div className="flex justify-end">
          <button
            type="button"
            onClick={handleSave}
            disabled={!canSave}
            data-testid="exploration-save-btn"
            className="px-4 py-2 bg-sky-600 hover:bg-sky-500 text-xs font-bold text-white rounded-lg flex items-center gap-2 transition disabled:opacity-50"
          >
            {saving ? <RotateCw className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
            <span>Zapisz eksplorację</span>
          </button>
        </div>
      </div>

      {/* Section: Planowane (muted) */}
      <div className="bg-surface/60 border border-subtle rounded-xl p-5 space-y-3" data-testid="planned-section">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-400">
          <Lock className="w-4 h-4 text-slate-500" />
          <span>Planowane</span>
        </div>
        <p className="text-[11px] text-slate-500">Jeszcze niedostępne. Nie wpływają na działanie AAW.</p>
        <ul className="space-y-1" data-testid="planned-list">
          {PLANNED_ITEMS.map((item) => (
            <li key={item} className="text-xs text-slate-500 flex items-center gap-2">
              <span className="w-1 h-1 rounded-full bg-slate-600 shrink-0" />
              <span>{item}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
};
