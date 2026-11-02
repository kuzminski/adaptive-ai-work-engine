/**
 * AAW Agentic Studio — New Task Wizard View
 *
 * Step-by-step or unified wizard to configure and launch a new autonomous run:
 * - Section 1: Repository selection, folder picker, and Git inspection
 * - Section 2: Goal, initial iteration focus, and roadmap directions
 * - Section 3: Model and role selection (Planner, Implementer, Implementer Chain)
 * - Section 4: Autonomy limits (iteration caps, preset, continuation mode)
 * - Section 5: Real-time validation and task dispatch via `/api/tasks/start`
 */

import React, { useState, useEffect } from "react";
import {
  FolderGit2,
  Sparkles,
  Bot,
  Cpu,
  Play,
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  RotateCw,
  FolderOpen,
  ArrowRight,
  Sliders,
} from "lucide-react";
import { client } from "../../api/client";
import { RepoInspectResult } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";

export const NewTaskWizardView: React.FC = () => {
  const {
    bootstrap,
    activePolicyPreset,
    setActivePolicyPreset,
    startRun,
    setActiveNav,
  } = useEngineStore();

  // Form State
  const defaultRepo =
    bootstrap?.recent_repos && bootstrap.recent_repos.length > 0
      ? bootstrap.recent_repos[0]
      : "";
  const [repoPath, setRepoPath] = useState<string>(defaultRepo);
  const [inspecting, setInspecting] = useState<boolean>(false);
  const [inspectResult, setInspectResult] = useState<RepoInspectResult | null>(null);
  const [inspectError, setInspectError] = useState<string | null>(null);

  const [goal, setGoal] = useState<string>("");
  const [firstIteration, setFirstIteration] = useState<string>("");
  const [directionsText, setDirectionsText] = useState<string>(
    "1. Zweryfikuj stan repozytorium i uruchom istniejące testy bazowe\n2. Zaimplementuj wymagane zmiany w odizolowanym worktree\n3. Dodaj kompleksowe testy jednostkowe i integracyjne\n4. Sprawdź kryteria akceptacji przed przekazaniem do bramki Human Gate"
  );

  const [maxIterations, setMaxIterations] = useState<number>(
    bootstrap?.limits?.default_max_iterations || 5
  );
  const [continueAutonomously, setContinueAutonomously] = useState<boolean>(true);

  // Model selections
  const [planningModel, setPlanningModel] = useState<string>("");
  const [implementerModel, setImplementerModel] = useState<string>("");

  const [submitting, setSubmitting] = useState<boolean>(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Initialize models from bootstrap
  useEffect(() => {
    if (bootstrap?.choices) {
      if (bootstrap.choices.planning && !planningModel) {
        setPlanningModel(bootstrap.choices.planning);
      }
      if (bootstrap.choices.implementation && !implementerModel) {
        setImplementerModel(bootstrap.choices.implementation);
      }
    }
  }, [bootstrap]);

  // Inspect repo when default repo is set
  useEffect(() => {
    if (repoPath && !inspectResult && !inspecting) {
      handleInspect(repoPath);
    }
  }, [repoPath]);

  const handleInspect = async (path: string) => {
    if (!path.trim()) return;
    setInspecting(true);
    setInspectError(null);
    try {
      const res = await client.inspectRepo(path.trim());
      setInspectResult(res);
      if (res.error) {
        setInspectError(res.error);
      }
    } catch (err: any) {
      setInspectError(err?.message || "Nie udało się zbadać repozytorium");
      setInspectResult(null);
    } finally {
      setInspecting(false);
    }
  };

  const handlePickFolder = async () => {
    try {
      const res = await client.pickFolder();
      if (res.path) {
        setRepoPath(res.path);
        await handleInspect(res.path);
      }
    } catch (err: any) {
      setInspectError(err?.message || "Wybór folderu został anulowany");
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!goal.trim() || goal.trim().length < 5) {
      setSubmitError("Podaj cel zadania (minimum 5 znaków).");
      return;
    }
    if (!repoPath.trim()) {
      setSubmitError("Wskaż ścieżkę do repozytorium.");
      return;
    }

    setSubmitting(true);
    setSubmitError(null);

    const directions = directionsText
      .split("\n")
      .map((d) => d.replace(/^\s*\d+[\.\)]\s*/, "").trim())
      .filter(Boolean);

    const formPayload = {
      repo: repoPath.trim(),
      goal: goal.trim(),
      first_iteration: firstIteration.trim() || undefined,
      directions: directions.length > 0 ? directions : undefined,
      planning: planningModel || undefined,
      implementation: implementerModel || undefined,
      preset: activePolicyPreset,
      advanced: {
        max_iterations: Number(maxIterations),
        continue_autonomously: continueAutonomously,
      },
    };

    try {
      await startRun({ form: formPayload });
      setActiveNav("command-center");
    } catch (err: any) {
      setSubmitError(err?.message || "Błąd uruchamiania zadania");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-4xl w-full mx-auto space-y-6 animate-fade-in pb-12" data-testid="new-task-wizard-view">
      {/* Header */}
      <div className="bg-surface border border-subtle rounded-xl p-5 shadow-xl flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs font-mono text-sky-400 uppercase mb-1">
            <Sparkles className="w-3.5 h-3.5" />
            <span>Kreator Nowego Zadania • 7-Phase Orchestrator</span>
          </div>
          <h2 className="text-xl font-bold tracking-tight text-white">
            Konfiguracja Zadania Autonomicznego
          </h2>
          <p className="text-xs text-slate-400 mt-1 max-w-2xl">
            Skonfiguruj cel, przestrzeń roboczą oraz limity pętli. Silnik automatycznie stworzy odizolowane środowisko Git i przeprowadzi zadanie przez fazy planowania, implementacji, weryfikacji i bramki Human Gate.
          </p>
        </div>
      </div>

      {submitError && (
        <div className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-xl flex items-center gap-3 text-rose-300 text-xs">
          <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
          <span>{submitError}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Section 1: Workspace & Repository */}
        <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <FolderGit2 className="w-4 h-4 text-sky-400" />
              <span>1. Repozytorium i Przestrzeń Robocza (Workspace)</span>
            </div>
            <span className="text-[11px] font-mono text-slate-400">Izolacja Git Fail-Closed</span>
          </div>

          <div className="space-y-2">
            <label className="text-xs font-medium text-slate-300 block">
              Ścieżka do repozytorium Git na dysku lokalnym:
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
                placeholder="np. C:\Users\projekt lub /home/user/projekt"
                className="flex-1 bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs font-mono text-white placeholder-slate-500 focus:outline-none focus:border-sky-500"
              />
              <button
                type="button"
                onClick={handlePickFolder}
                className="px-3 py-2 bg-elevated hover:bg-slate-700 border border-subtle rounded-lg text-xs font-medium text-slate-200 flex items-center gap-1.5 transition shrink-0"
              >
                <FolderOpen className="w-3.5 h-3.5 text-slate-400" />
                <span>Przeglądaj...</span>
              </button>
              <button
                type="button"
                onClick={() => handleInspect(repoPath)}
                disabled={inspecting || !repoPath.trim()}
                className="px-3 py-2 bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/40 rounded-lg text-xs font-medium text-sky-300 flex items-center gap-1.5 transition shrink-0 disabled:opacity-50"
              >
                <RotateCw className={`w-3.5 h-3.5 ${inspecting ? "animate-spin" : ""}`} />
                <span>Zbadaj</span>
              </button>
            </div>
          </div>

          {/* Recent Repos Quick Select */}
          {bootstrap?.recent_repos && bootstrap.recent_repos.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap pt-1">
              <span className="text-[11px] text-slate-500">Ostatnie repozytoria:</span>
              {bootstrap.recent_repos.slice(0, 3).map((r, idx) => (
                <button
                  key={idx}
                  type="button"
                  onClick={() => {
                    setRepoPath(r);
                    handleInspect(r);
                  }}
                  className="px-2 py-0.5 bg-elevated hover:bg-slate-700 border border-subtle rounded text-[11px] font-mono text-slate-300 transition"
                >
                  {r.split(/[\\/]/).filter(Boolean).pop() || r}
                </button>
              ))}
            </div>
          )}

          {/* Repo Inspection Feedback */}
          {inspectResult && (
            <div
              className={`p-3 rounded-lg border text-xs flex items-center justify-between ${
                inspectResult.is_git
                  ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-300"
                  : "bg-amber-500/10 border-amber-500/30 text-amber-300"
              }`}
            >
              <div className="flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4 shrink-0" />
                <span>
                  {inspectResult.is_git
                    ? `Prawidłowe repozytorium Git (gałąź: ${inspectResult.current_branch || "main"}${
                        inspectResult.dirty ? ", niezatwierdzone zmiany w drzewie" : ", czysty stan roboczy"
                      })`
                    : "Wskazana ścieżka nie jest repozytorium Git. Silnik może zainicjalizować git init."}
                </span>
              </div>
              {inspectResult.head_commit && (
                <span className="font-mono text-[10px] text-slate-400">
                  HEAD: {inspectResult.head_commit.slice(0, 7)}
                </span>
              )}
            </div>
          )}

          {inspectError && (
            <div className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-lg text-xs text-rose-300 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0 text-rose-400" />
              <span>{inspectError}</span>
            </div>
          )}
        </div>

        {/* Section 2: Goal & Direction */}
        <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <Sparkles className="w-4 h-4 text-sky-400" />
              <span>2. Cel Główny i Kierunek Iteracji</span>
            </div>
            <span className="text-[11px] font-mono text-slate-400">Deklaratywny kontrakt zadania</span>
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-300 flex items-center justify-between">
              <span>Co chcesz zbudować lub naprawić? (Cel zadania) *</span>
              <span className="text-[11px] text-slate-500 font-mono">Wymagane</span>
            </label>
            <textarea
              rows={3}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="np. Zaimplementuj moduł weryfikacji płatności Stripe wraz z testami integracyjnymi i obsługą ponowień webhooków."
              className="w-full bg-elevated border border-subtle rounded-lg p-3 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-sky-500 leading-relaxed"
              required
            />
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-300 block">
              Skupienie pierwszej iteracji (opcjonalnie):
            </label>
            <input
              type="text"
              value={firstIteration}
              onChange={(e) => setFirstIteration(e.target.value)}
              placeholder="np. Napisz testy kontraktowe i szkielet interfejsu przed kodem produkcyjnym"
              className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-sky-500"
            />
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-300 flex items-center justify-between">
              <span>Kroki planu / Roadmapa (kolejne punkty na osobnych liniach):</span>
              <span className="text-[11px] text-slate-500 font-mono">Weryfikowane w Fazie 7</span>
            </label>
            <textarea
              rows={4}
              value={directionsText}
              onChange={(e) => setDirectionsText(e.target.value)}
              placeholder="1. Punkt pierwszy&#10;2. Punkt drugi&#10;3. Punkt trzeci"
              className="w-full bg-elevated border border-subtle rounded-lg p-3 text-xs font-mono text-slate-200 placeholder-slate-500 focus:outline-none focus:border-sky-500 leading-relaxed"
            />
          </div>
        </div>

        {/* Section 3: Model & Role Configuration */}
        <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <Bot className="w-4 h-4 text-sky-400" />
              <span>3. Przydział Modeli AI i Ról Wykonawczych</span>
            </div>
            <span className="text-[11px] font-mono text-slate-400">Adaptacyjny Model Router</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 flex items-center gap-1.5">
                <Bot className="w-3.5 h-3.5 text-indigo-400" />
                <span>Model Planera (Fazy 1, 2, 7)</span>
              </label>
              <select
                value={planningModel}
                onChange={(e) => setPlanningModel(e.target.value)}
                className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500"
              >
                <option value="">-- Domyślny inteligentny planer (Zalecany) --</option>
                <option value="claude-3-7-sonnet">Claude 3.7 Sonnet (Architektura / Reasoning)</option>
                <option value="gemini-2.5-pro">Gemini 2.5 Pro (Głęboka analiza kodu)</option>
                <option value="codex-o3-mini">Codex o3-mini (Precyzyjna logika algorytmiczna)</option>
              </select>
            </div>

            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 flex items-center gap-1.5">
                <Cpu className="w-3.5 h-3.5 text-emerald-400" />
                <span>Model Implementatora (Fazy 3, 4, 5, 6)</span>
              </label>
              <select
                value={implementerModel}
                onChange={(e) => setImplementerModel(e.target.value)}
                className="w-full bg-elevated border border-subtle rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500"
              >
                <option value="">-- Domyślny szybki wykonawca (Zalecany) --</option>
                <option value="claude-3-5-sonnet">Claude 3.5 Sonnet (Wysoka precyzja refaktoryzacji)</option>
                <option value="gemini-2.5-flash">Gemini 2.5 Flash (Szybkie i ekonomiczne mikro-zadania)</option>
                <option value="claude-3-5-haiku">Claude 3.5 Haiku (Lokalne formatowanie i lintery)</option>
              </select>
            </div>
          </div>
        </div>

        {/* Section 4: Autonomy Limits & Presets */}
        <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex items-center justify-between border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              <Sliders className="w-4 h-4 text-sky-400" />
              <span>4. Limity Autonomii i Polityka Bezpieczeństwa</span>
            </div>
            <span className="text-[11px] font-mono text-slate-400">Reguły Pętli i Bramki</span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {(
              [
                {
                  id: "conservative",
                  title: "Konserwatywny",
                  desc: "Zatrzymuje się na bramce Human Gate po każdej iteracji",
                },
                {
                  id: "balanced",
                  title: "Zrównoważony",
                  desc: "Prowadzi pętlę do zakończenia roadmapy lub istotnego błędu",
                },
                {
                  id: "autonomous",
                  title: "Autonomiczny",
                  desc: "Prowadzi pełną samonaprawę (repair loop) do zielonych testów",
                },
              ] as const
            ).map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => setActivePolicyPreset(p.id)}
                className={`p-3 rounded-lg border text-left transition ${
                  activePolicyPreset === p.id
                    ? "bg-sky-500/10 border-sky-500/50 text-white shadow-md"
                    : "bg-elevated/40 border-subtle text-slate-400 hover:text-slate-200"
                }`}
              >
                <div className="text-xs font-bold text-sky-300 mb-1">{p.title}</div>
                <div className="text-[11px] leading-tight text-slate-400">{p.desc}</div>
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
            <div className="space-y-1">
              <label className="text-xs font-medium text-slate-300 flex items-center justify-between">
                <span>Maksymalna liczba iteracji silnika:</span>
                <span className="font-mono text-sky-400 text-xs">{maxIterations}</span>
              </label>
              <input
                type="range"
                min={1}
                max={15}
                value={maxIterations}
                onChange={(e) => setMaxIterations(Number(e.target.value))}
                className="w-full accent-sky-500 cursor-pointer"
              />
              <div className="flex justify-between text-[10px] text-slate-500 font-mono">
                <span>1 (Pojedynczy krok)</span>
                <span>5 (Standard)</span>
                <span>15 (Złożone)</span>
              </div>
            </div>

            <div className="flex items-center pt-4">
              <label className="flex items-center gap-2.5 text-xs text-slate-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={continueAutonomously}
                  onChange={(e) => setContinueAutonomously(e.target.checked)}
                  className="rounded border-subtle bg-elevated text-sky-500 focus:ring-sky-500 w-4 h-4"
                />
                <span>Kontynuuj autonomicznie dopóki roadmapa oferuje uzasadnioną pracę</span>
              </label>
            </div>
          </div>
        </div>

        {/* Submit Action Bar */}
        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={() => setActiveNav("command-center")}
            className="px-4 py-2 bg-elevated hover:bg-slate-700 text-xs font-medium text-slate-300 border border-subtle rounded-lg transition"
          >
            Anuluj
          </button>

          <button
            type="submit"
            disabled={submitting}
            className="px-6 py-2.5 bg-sky-600 hover:bg-sky-500 text-xs font-bold text-white rounded-lg flex items-center gap-2 shadow-lg hover:shadow-sky-500/20 transition disabled:opacity-50"
            data-testid="submit-new-task-btn"
          >
            {submitting ? (
              <>
                <RotateCw className="w-4 h-4 animate-spin" />
                <span>Inicjalizowanie środowiska i zadania...</span>
              </>
            ) : (
              <>
                <Play className="w-4 h-4 text-white" />
                <span>Uruchom Zadanie Autonomiczne</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
};
