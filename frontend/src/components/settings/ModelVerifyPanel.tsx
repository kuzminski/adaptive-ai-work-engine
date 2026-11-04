/**
 * AAW Agentic Studio — Model & Tool Verification Panel (roadmap 2.6)
 *
 * Re-detects the locally installed AI CLIs (POST /api/providers/detect) and probes
 * the exact models behind the configured profiles (POST /api/models/verify).
 * Both actions run only on an explicit click. Labels and states are the ones the
 * backend returns (product_providers.py: AVAILABILITY_LABEL, login states, probe status).
 */

import React, { useState } from "react";
import { Cpu, Search, RotateCw, CheckCircle2, AlertCircle, XCircle, Info } from "lucide-react";
import { client } from "../../api/client";
import { useEngineStore } from "../../store/engineStore";

interface ProviderModel {
  runtime_model_id?: string | null;
  model_family?: string | null;
  state?: string | null;
  label?: string | null;
}

interface DetectedProvider {
  provider_id?: string;
  display_name?: string;
  harness?: string;
  status?: string; // FOUND | NOT_FOUND
  executable?: string | null;
  version?: string | null;
  login?: string | null; // LOGGED_IN | NOT_LOGGED_IN | UNKNOWN
  login_detail?: string | null;
  runnable_profiles?: string[];
  models?: ProviderModel[];
}

interface Detection {
  detected_at?: string;
  providers?: DetectedProvider[];
  usable_profile_count?: number;
}

interface VerifyResult {
  harness?: string;
  model?: string;
  status?: string; // ACCEPTED | REJECTED | UNKNOWN
  detail?: string | null;
  probed_at?: string | null;
}

interface VerifyResponse {
  results?: VerifyResult[];
  checked?: number;
  providers?: Detection;
}

const PROVIDER_STATUS_LABEL: Record<string, string> = {
  FOUND: "wykryte",
  NOT_FOUND: "nie wykryto",
};

const LOGIN_LABEL: Record<string, string> = {
  LOGGED_IN: "zalogowane",
  NOT_LOGGED_IN: "niezalogowane",
  UNKNOWN: "nieznane (nie da się bezpiecznie ustalić)",
};

const PROBE_STATUS_LABEL: Record<string, string> = {
  ACCEPTED: "CLI zaakceptowało model",
  REJECTED: "CLI odrzuciło model",
  UNKNOWN: "nie udało się ustalić",
};

// Availability states as defined in product_providers.py (RUNNABLE_STATES and the blocked ones).
const RUNNABLE_STATES = ["AVAILABLE", "NOT_VERIFIED", "VERIFIED_HERE"];
const BLOCKED_STATES = ["REJECTED_HERE", "POLICY_UNAVAILABLE", "CLI_NOT_FOUND", "NOT_LOGGED_IN"];

function modelTone(state?: string | null): string {
  if (!state) return "text-slate-400";
  if (RUNNABLE_STATES.includes(state)) return "text-emerald-400";
  if (state === "NEEDS_CHECK") return "text-amber-400";
  if (BLOCKED_STATES.includes(state)) return "text-rose-400";
  return "text-slate-400";
}

export const ModelVerifyPanel: React.FC = () => {
  const { bootstrap, loadBootstrap } = useEngineStore();

  const [detection, setDetection] = useState<Detection | null>(null);
  const [results, setResults] = useState<VerifyResult[] | null>(null);
  const [detecting, setDetecting] = useState<boolean>(false);
  const [verifying, setVerifying] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Fresh local detection wins; otherwise the last detection the backend put into bootstrap.
  const current: Detection | null =
    detection ?? ((bootstrap?.providers as unknown) as Detection | null) ?? null;
  const providers: DetectedProvider[] = current?.providers ?? [];
  const busy = detecting || verifying;

  const handleDetect = async () => {
    setDetecting(true);
    setError(null);
    try {
      const res = (await client.detectProviders()) as Detection;
      setDetection(res);
      await loadBootstrap();
    } catch (err: any) {
      setError(err?.message || "Nie udało się ponownie wykryć narzędzi AI");
    } finally {
      setDetecting(false);
    }
  };

  const handleVerify = async () => {
    setVerifying(true);
    setError(null);
    try {
      const res = (await client.verifyModels({})) as VerifyResponse;
      setResults(Array.isArray(res?.results) ? res.results : []);
      if (res?.providers) {
        setDetection(res.providers);
      }
    } catch (err: any) {
      setError(err?.message || "Nie udało się sprawdzić modeli");
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div
      className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4"
      data-testid="model-verify-panel"
    >
      <div className="flex items-center justify-between border-b border-subtle pb-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <Cpu className="w-4 h-4 text-sky-400" />
          <span>Narzędzia AI i sprawdzenie modeli</span>
        </div>
        <span className="text-[11px] font-mono text-slate-400">Stan na tym komputerze</span>
      </div>

      <p className="text-xs text-slate-300 leading-relaxed">
        AAW korzysta z lokalnie zainstalowanych CLI. „Wykryj ponownie” sprawdza, które CLI są dostępne i czy są
        zalogowane. „Sprawdź modele” wysyła do każdego modelu jedno krótkie zapytanie i zapisuje wynik na tym
        komputerze.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={handleDetect}
          disabled={busy}
          data-testid="detect-providers-btn"
          className="px-3 py-1.5 bg-elevated hover:bg-slate-700 text-xs font-medium text-sky-400 border border-subtle rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
        >
          {detecting ? <RotateCw className="w-3.5 h-3.5 animate-spin" /> : <Search className="w-3.5 h-3.5" />}
          <span>Wykryj ponownie narzędzia AI</span>
        </button>
        <button
          type="button"
          onClick={handleVerify}
          disabled={busy}
          data-testid="verify-models-btn"
          className="px-3 py-1.5 bg-elevated hover:bg-slate-700 text-xs font-medium text-sky-400 border border-subtle rounded-lg flex items-center gap-1.5 transition disabled:opacity-50"
        >
          {verifying ? <RotateCw className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
          <span>Sprawdź modele</span>
        </button>
      </div>

      {error && (
        <div
          className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-lg flex items-start gap-2 text-rose-300 text-xs"
          data-testid="model-verify-error"
        >
          <AlertCircle className="w-4 h-4 text-rose-400 shrink-0 mt-px" />
          <span className="break-words">{error}</span>
        </div>
      )}

      {providers.length === 0 ? (
        <p className="text-xs text-slate-500" data-testid="provider-empty">
          Brak danych o narzędziach AI. Kliknij „Wykryj ponownie narzędzia AI”, aby sprawdzić, co jest zainstalowane.
        </p>
      ) : (
        <div className="space-y-3" data-testid="provider-list">
          {typeof current?.usable_profile_count === "number" && (
            <p className="text-[11px] text-slate-400" data-testid="usable-profile-count">
              Profile gotowe do użycia: <span className="font-mono text-sky-400">{current.usable_profile_count}</span>
            </p>
          )}

          {providers.map((provider, index) => {
            const key = provider.provider_id || provider.harness || `provider-${index}`;
            const found = provider.status === "FOUND";
            const models = provider.models ?? [];
            return (
              <div
                key={key}
                className="p-3 bg-elevated/40 border border-subtle rounded-lg space-y-2"
                data-testid={`provider-${key}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-bold text-slate-200">{provider.display_name || provider.harness || key}</div>
                  <div className="flex flex-wrap items-center gap-3 text-[11px] font-mono">
                    <span className={found ? "text-emerald-400" : "text-rose-400"} data-testid={`provider-status-${key}`}>
                      {PROVIDER_STATUS_LABEL[provider.status ?? ""] ?? "stan nieznany"}
                    </span>
                    <span className="text-slate-400" data-testid={`provider-version-${key}`}>
                      {`wersja: ${provider.version || "—"}`}
                    </span>
                    <span className="text-slate-400" data-testid={`provider-login-${key}`}>
                      {`logowanie: ${LOGIN_LABEL[provider.login ?? ""] ?? "—"}`}
                    </span>
                  </div>
                </div>

                {provider.login_detail && (
                  <p className="text-[11px] text-slate-500">{provider.login_detail}</p>
                )}

                {models.length === 0 ? (
                  <p className="text-[11px] text-slate-500" data-testid={`provider-models-empty-${key}`}>
                    Brak modeli przypisanych do tego narzędzia.
                  </p>
                ) : (
                  <ul className="space-y-1" data-testid={`provider-models-${key}`}>
                    {models.map((model, mIndex) => (
                      <li
                        key={`${key}-${model.runtime_model_id || mIndex}`}
                        className="flex flex-wrap items-center justify-between gap-2 text-[11px]"
                        data-testid="model-row"
                      >
                        <span className="font-mono text-slate-300 break-all">{model.runtime_model_id || "—"}</span>
                        <span className={`font-medium ${modelTone(model.state)}`} data-testid="model-state-label">
                          {model.label || model.state || "—"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
        </div>
      )}

      {results !== null && (
        <div className="space-y-2" data-testid="verify-results">
          <div className="text-[11px] font-mono text-slate-400">Wynik sprawdzenia modeli</div>
          {results.length === 0 ? (
            <p className="text-xs text-slate-500 flex items-center gap-1.5" data-testid="verify-results-empty">
              <Info className="w-3.5 h-3.5 shrink-0" />
              <span>
                Nie było czego sprawdzić: brak zainstalowanych CLI albo modeli wymagających sprawdzenia.
              </span>
            </p>
          ) : (
            <ul className="space-y-1">
              {results.map((row, index) => {
                const accepted = row.status === "ACCEPTED";
                const rejected = row.status === "REJECTED";
                return (
                  <li
                    key={`${row.harness}-${row.model}-${index}`}
                    className="p-2 bg-elevated/40 border border-subtle rounded-lg text-[11px] space-y-0.5"
                    data-testid="verify-result-row"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-mono text-slate-300 break-all">
                        {row.harness || "—"} / {row.model || "—"}
                      </span>
                      <span
                        className={`flex items-center gap-1 font-medium ${
                          accepted ? "text-emerald-400" : rejected ? "text-rose-400" : "text-amber-400"
                        }`}
                      >
                        {accepted ? (
                          <CheckCircle2 className="w-3.5 h-3.5" />
                        ) : rejected ? (
                          <XCircle className="w-3.5 h-3.5" />
                        ) : (
                          <AlertCircle className="w-3.5 h-3.5" />
                        )}
                        <span>{PROBE_STATUS_LABEL[row.status ?? ""] ?? row.status ?? "—"}</span>
                      </span>
                    </div>
                    {row.detail && <p className="text-slate-500 break-words">{row.detail}</p>}
                    {row.probed_at && <p className="text-slate-600 font-mono">sprawdzono: {row.probed_at}</p>}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
};
