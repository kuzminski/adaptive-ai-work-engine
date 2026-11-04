/**
 * AAW Agentic Studio — Start forecast (before START).
 *
 * Shows what the user's own history says about the goal and the whole roadmap: horizon, cost range and the
 * implementation model that worked best for this kind of work. Nothing is applied automatically: the model
 * is changed only by an explicit click and can be undone.
 */

import React, { useEffect, useRef, useState } from "react";
import { Gauge, AlertTriangle, RotateCw, Undo2, CheckCircle2 } from "lucide-react";
import { client } from "../../api/client";
import { formatDuration, formatRange } from "./IdeaIntakePanel";

const DEBOUNCE_MS = 800;
const MIN_GOAL_CHARS = 5;

const CONFIDENCE_LABEL: Record<string, string> = {
  RELIABLE: "wiarygodne",
  PRELIMINARY: "wstępne",
  TOO_FEW: "za mało danych",
};

/** Pure renderer of a forecast response (`POST /api/experience/forecast`). Also used by tests. */
export const ForecastView: React.FC<{
  data: any | null;
  loading?: boolean;
  error?: string | null;
  appliedProfileId: string | null;
  onApplyRecommended: (profileId: string, rec?: any) => void;
  onUndoRecommended: () => void;
}> = ({ data, loading = false, error = null, appliedProfileId, onApplyRecommended, onUndoRecommended }) => {
  if (error) {
    return (
      <div className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-lg text-xs text-rose-300 flex items-center gap-2" data-testid="start-forecast-error">
        <AlertTriangle className="w-4 h-4 shrink-0 text-rose-400" />
        <span>{error}</span>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="text-xs text-slate-500 flex items-center gap-2" data-testid="start-forecast-empty">
        {loading ? (
          <>
            <RotateCw className="w-3.5 h-3.5 animate-spin" />
            <span data-testid="start-forecast-loading">Liczę prognozę z Twojej historii...</span>
          </>
        ) : (
          <span>Wpisz cel (min. {MIN_GOAL_CHARS} znaków), a pokażę horyzont, koszt i model z Twojej historii.</span>
        )}
      </div>
    );
  }

  const roadmap = data.roadmap || null;
  const total = roadmap?.total || null;
  const costText = total ? formatRange(total.cost, total.unit) : null;
  const itemCostText = data.per_iteration
    ? formatRange(data.per_iteration, data.unit)
    : null;
  const wallText = total?.wall_s && typeof total.wall_s.mid === "number" ? formatDuration(total.wall_s.mid) : null;
  const rec = data.recommended || null;
  const apply = rec?.apply || null;
  const isApplied = !!rec && appliedProfileId === rec.profile_id;
  const canApply = !!rec && !!apply && apply.differs && apply.runnable && !isApplied;

  return (
    <div className="space-y-3 text-xs text-slate-300" data-testid="start-forecast-data">
      <p data-testid="start-forecast-horizon">
        {roadmap?.reach?.text || "Za mało zakończonych zadań, by oszacować szansę dojścia do końca."}
      </p>

      <p data-testid="start-forecast-cost">
        {costText
          ? `Koszt całego planu (p25–p75): ${costText}${wallText ? `, czas ok. ${wallText}` : ""}`
          : "Za mało danych z historii, by podać koszt całego planu."}
      </p>
      {itemCostText && (
        <p className="text-slate-400" data-testid="start-forecast-per-iteration">
          Jedna iteracja tego rodzaju: {itemCostText}
        </p>
      )}
      {total && typeof total.iterations === "number" && (
        <p className="text-slate-400" data-testid="start-forecast-iterations">
          Iteracje: {total.iterations}
          {total.human_items ? `, punkty dla człowieka: ${total.human_items}` : ""}
        </p>
      )}
      {Array.isArray(roadmap?.warnings) &&
        roadmap.warnings.map((w: string, idx: number) => (
          <p key={idx} className="text-amber-300 flex items-start gap-1.5" data-testid="start-forecast-warning">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
            <span>{w}</span>
          </p>
        ))}
      {data.note && <p className="text-slate-500" data-testid="start-forecast-note">{data.note}</p>}

      <div className="pt-2 border-t border-subtle space-y-2" data-testid="start-forecast-model">
        <div className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-400 uppercase">
          <Gauge className="w-3.5 h-3.5" />
          <span>Model do implementacji dla: {data.kind_label || "tego zadania"}</span>
        </div>
        {rec ? (
          <p data-testid="start-forecast-recommended">
            Najlepszy dotąd: <span className="text-white">{rec.label || rec.profile_id}</span>, skuteczność{" "}
            {typeof rec.rate === "number" ? `${Math.round(rec.rate * 100)}%` : "–"} w {rec.n ?? 0} próbach (
            {CONFIDENCE_LABEL[rec.confidence] || rec.confidence || "brak oceny"})
          </p>
        ) : (
          <p className="text-slate-500" data-testid="start-forecast-no-recommendation">
            Za mało danych, by wskazać model dla tego rodzaju pracy. Zostaje Twój wybór.
          </p>
        )}

        {rec && apply && !apply.differs && !isApplied && (
          <p className="text-slate-500" data-testid="start-forecast-already-best">Wybrany model już jest rekomendowany.</p>
        )}
        {rec && apply && !apply.runnable && (
          <p className="text-amber-300" data-testid="start-forecast-not-runnable">
            Rekomendowany model nie jest teraz dostępny na tym komputerze.
          </p>
        )}

        {canApply && (
          <button
            type="button"
            onClick={() => onApplyRecommended(rec.profile_id, rec)}
            className="px-3 py-1.5 bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/40 rounded-lg text-xs font-medium text-sky-300 transition"
            data-testid="apply-recommended-model-btn"
          >
            Użyj rekomendowanego modelu dla tego rodzaju pracy
          </button>
        )}

        {isApplied && (
          <div className="flex items-center gap-3">
            <p className="text-emerald-300 flex items-center gap-1.5" data-testid="applied-recommended-model-note">
              <CheckCircle2 className="w-3.5 h-3.5" />
              <span>Ustawiono rekomendowany model.</span>
            </p>
            <button
              type="button"
              onClick={onUndoRecommended}
              className="px-2.5 py-1 bg-elevated hover:bg-slate-700 border border-subtle rounded-lg text-[11px] text-slate-300 flex items-center gap-1 transition"
              data-testid="undo-recommended-model-btn"
            >
              <Undo2 className="w-3 h-3" />
              <span>Cofnij</span>
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export const StartForecast: React.FC<{
  goal: string;
  firstIteration: string;
  directions: string[];
  chainMode: boolean;
  currentProfileId: string;
  appliedProfileId: string | null;
  onApplyRecommended: (profileId: string, rec?: any) => void;
  onUndoRecommended: () => void;
}> = ({
  goal,
  firstIteration,
  directions,
  chainMode,
  currentProfileId,
  appliedProfileId,
  onApplyRecommended,
  onUndoRecommended,
}) => {
  const [state, setState] = useState<{ loading: boolean; data: any | null; error: string | null }>({
    loading: false,
    data: null,
    error: null,
  });
  const requestSeq = useRef(0);

  const key = JSON.stringify({
    goal: goal.trim(),
    firstIteration: firstIteration.trim(),
    directions,
    chainMode,
    currentProfileId,
  });

  useEffect(() => {
    if (goal.trim().length < MIN_GOAL_CHARS) {
      requestSeq.current += 1;
      setState({ loading: false, data: null, error: null });
      return;
    }
    const seq = ++requestSeq.current;
    setState((prev) => ({ ...prev, loading: true }));
    const timer = setTimeout(async () => {
      try {
        const data = await client.postForecast({
          goal: goal.trim(),
          first_iteration: firstIteration.trim() || undefined,
          directions,
          chain_mode: chainMode,
          // the wizard holds a level (e.g. RECOMMENDED); the backend resolves it to the profile that would run
          implementation: currentProfileId || undefined,
        });
        if (seq === requestSeq.current) setState({ loading: false, data, error: null });
      } catch (err: any) {
        if (seq === requestSeq.current) {
          setState({ loading: false, data: null, error: err?.message || "Nie udało się policzyć prognozy." });
        }
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return (
    <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-3" data-testid="start-forecast">
      <div className="flex items-center justify-between border-b border-subtle pb-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <Gauge className="w-4 h-4 text-sky-400" />
          <span>Prognoza przed startem</span>
        </div>
        <span className="text-[11px] font-mono text-slate-400">Z Twojej historii, nic nie startuje</span>
      </div>
      <ForecastView
        data={state.data}
        loading={state.loading}
        error={state.error}
        appliedProfileId={appliedProfileId}
        onApplyRecommended={onApplyRecommended}
        onUndoRecommended={onUndoRecommended}
      />
    </div>
  );
};
