/**
 * AAW Agentic Studio — Live Chain Panel
 *
 * Live view of a running chain (roadmap stage 2.3). Renders `run.live` from
 * GET /api/runs/{id} (built by aaw_experience.live_view):
 * - journey: idea -> plan -> chains -> review serii -> polerowanie -> decyzja (stepper)
 * - chain: slots of the current chain (done / provisional / active / pending), closing slot = CHAIN_CLOSE review
 * - meter: spent so far (USD when priced, otherwise proxy units) with its basis label
 * - forecast: low-mid-high range for the rest of the run, or the backend's honest "no estimate" note
 * - deferred minor findings ("odłożone na polerowanie") and the collapsible live feed
 *
 * Every field may be missing or null; nothing is invented. A `live.error` shows a muted note.
 */

import React from "react";
import { Route, Coins, Gauge, Layers, ListChecks, Activity, ChevronRight } from "lucide-react";

export interface LiveChainPanelProps {
  live: any;
  status?: string;
}

type Tone = "done" | "active" | "pending" | "failed" | "provisional";

const TONE_CLASS: Record<Tone, string> = {
  done: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  active: "border-sky-500/50 bg-sky-500/15 text-sky-200",
  provisional: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  pending: "border-subtle bg-elevated/40 text-slate-400",
  failed: "border-rose-500/40 bg-rose-500/10 text-rose-300",
};

const TONE_DOT: Record<Tone, string> = {
  done: "bg-emerald-400",
  active: "bg-sky-400 animate-pulse",
  provisional: "bg-amber-400",
  pending: "bg-slate-600",
  failed: "bg-rose-400",
};

const SLOT_TONE: Record<string, { tone: Tone; label: string }> = {
  accepted: { tone: "done", label: "zrobione" },
  provisional: { tone: "provisional", label: "wstępnie" },
  active: { tone: "active", label: "w toku" },
  pending: { tone: "pending", label: "czeka" },
  failed: { tone: "failed", label: "zatrzymane" },
};

const JOURNEY_TONE: Record<string, Tone> = {
  done: "done",
  active: "active",
  pending: "pending",
  failed: "failed",
};

const num = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function fmtUsd(v: number): string {
  return `$${v.toFixed(4)}`;
}

export function fmtAmount(v: number, unit: string | null | undefined): string {
  return unit === "USD" ? fmtUsd(v) : `${Math.round(v).toLocaleString("pl-PL")} proxy`;
}

export function fmtDuration(seconds: unknown): string {
  if (!num(seconds)) return "—";
  const t = Math.round(seconds);
  if (t < 60) return `${t}s`;
  const m = Math.floor(t / 60);
  if (m < 60) return `${m} min ${t % 60}s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

function fmtTime(at: unknown): string {
  return typeof at === "string" && at.length >= 19 ? at.slice(11, 19) : "";
}

const SOURCE_LABEL: Record<string, string> = {
  "TEN PRZEBIEG": "ten przebieg",
  HISTORIA: "Twoja historia",
};

export const LiveChainPanel: React.FC<LiveChainPanelProps> = ({ live, status }) => {
  if (!live || typeof live !== "object") {
    return null;
  }

  if (live.error) {
    return (
      <section
        data-testid="live-chain-panel"
        aria-label="Podgląd na żywo"
        className="bg-surface border border-subtle rounded-xl p-4 shadow-xl"
      >
        <p data-testid="live-error-note" className="text-xs font-mono text-slate-500">
          Podgląd na żywo chwilowo niedostępny ({String(live.error)}).
        </p>
      </section>
    );
  }

  const journey: any[] = Array.isArray(live.journey) ? live.journey : [];
  const chain = live.chain && typeof live.chain === "object" ? live.chain : null;
  const slots: any[] = chain && Array.isArray(chain.slots) ? chain.slots : [];
  const meter = live.meter && typeof live.meter === "object" ? live.meter : null;
  const forecast = live.forecast && typeof live.forecast === "object" ? live.forecast : null;
  const estimate = forecast && forecast.estimate ? forecast.estimate : null;
  const feed: any[] = Array.isArray(live.feed) ? live.feed : [];
  const deferredOpen = num(live.deferred_open) ? live.deferred_open : 0;
  const now = live.now && typeof live.now === "object" ? live.now : null;
  const polishing = journey.some((step) => step?.id === "polish" && step?.state === "active");

  const spentUsd = meter && num(meter.cost_usd) ? meter.cost_usd : null;
  const spentUnits = meter && num(meter.proxy_units) ? meter.proxy_units : null;
  const spentUnit = spentUsd !== null ? "USD" : "proxy";
  const spent = spentUsd !== null ? spentUsd : spentUnits;

  return (
    <section
      data-testid="live-chain-panel"
      aria-label="Podgląd na żywo"
      className="bg-surface border border-subtle rounded-xl p-5 shadow-xl space-y-5"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-bold text-white flex items-center gap-2">
          <Route className="w-4 h-4 text-sky-400" />
          <span>Łańcuch na żywo</span>
        </h3>
        <div className="flex items-center gap-2 text-[10px] font-mono text-slate-400">
          {status && <span data-testid="live-status">{status}</span>}
          {polishing && (
            <span data-testid="live-polish-badge" className="px-2 py-0.5 rounded border border-indigo-500/40 text-indigo-300">
              POLISH
            </span>
          )}
        </div>
      </header>

      {/* Journey stepper */}
      <div data-testid="live-journey" className="space-y-2">
        {journey.length === 0 ? (
          <p data-testid="live-journey-empty" className="text-xs text-slate-500">
            Za mało danych, by pokazać etapy przebiegu.
          </p>
        ) : (
          <ol className="flex flex-wrap items-stretch gap-2">
            {journey.map((step, index) => {
              const tone = JOURNEY_TONE[step?.state] || "pending";
              return (
                <li
                  key={String(step?.id ?? index)}
                  data-testid={`live-journey-step-${step?.id ?? index}`}
                  data-state={step?.state ?? "pending"}
                  className={`flex items-center gap-2 px-2.5 py-1.5 rounded-lg border text-xs ${TONE_CLASS[tone]}`}
                >
                  <span className={`w-2 h-2 rounded-full shrink-0 ${TONE_DOT[tone]}`} />
                  <span className="font-semibold">{step?.label ?? "Etap"}</span>
                  {step?.detail && <span className="text-[10px] font-mono opacity-70">{step.detail}</span>}
                  {index < journey.length - 1 && <ChevronRight className="w-3 h-3 text-slate-600 shrink-0" />}
                </li>
              );
            })}
          </ol>
        )}
      </div>

      {/* Current chain slots */}
      <div data-testid="live-chain" className="space-y-2">
        <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5">
          <Layers className="w-3 h-3" />
          <span>
            {chain
              ? `Łańcuch ${chain.chain_id ?? ""} — ${num(chain.closed) ? chain.closed : 0} zamkniętych`
              : "Łańcuch"}
          </span>
        </div>
        {slots.length === 0 ? (
          <p data-testid="live-chain-empty" className="text-xs text-slate-500">
            Ten przebieg nie używa łańcucha iteracji.
          </p>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            {slots.map((slot, index) => {
              const mapped = SLOT_TONE[slot?.state] || SLOT_TONE.pending;
              const position = slot?.position ?? index + 1;
              const review = slot?.closing ? "CHAIN_CLOSE" : "LIGHT";
              return (
                <div
                  key={`slot-${position}`}
                  data-testid={`live-chain-slot-${position}`}
                  data-state={slot?.state ?? "pending"}
                  data-review-mode={review}
                  className={`p-2.5 rounded-lg border text-xs space-y-1 ${TONE_CLASS[mapped.tone]}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono font-bold">#{position}</span>
                    <span className="text-[10px] font-mono uppercase">{mapped.label}</span>
                  </div>
                  <div className="text-[10px] font-mono opacity-80">
                    {review}
                    {slot?.explored ? " · eksploracja" : ""}
                  </div>
                  {slot?.goal && <div className="text-[11px] text-slate-300 truncate" title={String(slot.goal)}>{String(slot.goal)}</div>}
                  {num(slot?.cost) && <div className="text-[10px] font-mono">{fmtAmount(slot.cost, spentUnit)}</div>}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {now && (
        <p data-testid="live-now" className="text-xs text-slate-300 font-mono">
          Teraz: {[now.role, now.executor, now.profile].filter(Boolean).join(" · ") || "w toku"}
        </p>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {/* Cost / time meter */}
        <div data-testid="live-meter" className="bg-elevated/40 p-3 rounded-lg border border-subtle space-y-1.5">
          <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5">
            <Coins className="w-3 h-3" />
            <span>Wydano do tej pory</span>
          </div>
          {meter && meter.calls ? (
            <>
              <div data-testid="live-meter-spent" className="text-sm font-mono font-bold text-slate-100">
                {spent !== null ? fmtAmount(spent, spentUnit) : "brak wyceny"}
              </div>
              <div data-testid="live-meter-basis" className="text-[10px] font-mono text-slate-400">
                {`jednostka: ${spentUnit}${spentUnit === "proxy" ? " (brak ceny w USD)" : ""}`}
              </div>
              <div className="text-[11px] font-mono text-slate-400">
                wywołania: {meter.calls}
                {num(meter.failed_calls) && meter.failed_calls > 0 ? ` · błędy: ${meter.failed_calls}` : ""}
                {num(meter.unpriced_calls) && meter.unpriced_calls > 0 ? ` · bez wyceny: ${meter.unpriced_calls}` : ""}
              </div>
              <div data-testid="live-meter-wall" className="text-[11px] font-mono text-slate-400 flex items-center gap-1">
                <Gauge className="w-3 h-3" />
                <span>czas: {fmtDuration(meter.wall_s)}</span>
              </div>
            </>
          ) : (
            <p data-testid="live-meter-empty" className="text-xs text-slate-500">
              Za mało danych — licznik ruszy po pierwszych wywołaniach.
            </p>
          )}
        </div>

        {/* Forecast */}
        <div data-testid="live-forecast" className="bg-elevated/40 p-3 rounded-lg border border-subtle space-y-1.5">
          <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5">
            <Activity className="w-3 h-3" />
            <span>Prognoza na resztę przebiegu</span>
          </div>
          {estimate && estimate.cost ? (
            <>
              <div data-testid="live-forecast-cost" className="text-sm font-mono font-bold text-slate-100">
                {fmtAmount(estimate.cost.low, estimate.unit)} – {fmtAmount(estimate.cost.high, estimate.unit)}
              </div>
              <div className="text-[11px] font-mono text-slate-400">
                mediana {fmtAmount(estimate.cost.mid, estimate.unit)} · punktów: {estimate.iterations_left ?? "—"}
              </div>
            </>
          ) : estimate && estimate.wall_s ? (
            <div data-testid="live-forecast-wall" className="text-sm font-mono font-bold text-slate-100">
              {fmtDuration(estimate.wall_s.low)} – {fmtDuration(estimate.wall_s.high)}
            </div>
          ) : null}
          {estimate && (
            <div className="text-[10px] font-mono text-slate-500">
              {`źródło: ${SOURCE_LABEL[estimate.source] || estimate.source || "—"} (n=${estimate.n ?? 0})`}
            </div>
          )}
          {!estimate && (
            <p data-testid="live-forecast-empty" className="text-xs text-slate-500">
              {forecast?.note || "Za mało danych, by uczciwie oszacować czas i koszt."}
            </p>
          )}
          {estimate && forecast?.note && (
            <p className="text-[10px] text-slate-500">{forecast.note}</p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span data-testid="live-deferred" className="flex items-center gap-1.5 text-slate-300">
          <ListChecks className="w-3.5 h-3.5 text-amber-400" />
          {deferredOpen > 0
            ? `Drobne uwagi odłożone na polerowanie: ${deferredOpen}`
            : "Brak drobnych uwag odłożonych na polerowanie"}
        </span>
      </div>

      {/* Live feed */}
      <details data-testid="live-feed" className="group rounded-lg border border-subtle bg-elevated/30 p-3">
        <summary className="cursor-pointer text-xs font-semibold text-slate-300 select-none">
          Ostatnie zdarzenia ({feed.length})
        </summary>
        {feed.length === 0 ? (
          <p data-testid="live-feed-empty" className="text-xs text-slate-500 pt-2">
            Brak zdarzeń.
          </p>
        ) : (
          <ul className="pt-2 space-y-1">
            {feed.map((item, index) => (
              <li
                key={`feed-${index}`}
                data-testid="live-feed-item"
                className="text-[11px] font-mono text-slate-400 flex items-center gap-2"
              >
                <span className="text-slate-600 shrink-0">{fmtTime(item?.at)}</span>
                <span className="text-slate-300">{item?.text ?? item?.event ?? ""}</span>
              </li>
            ))}
          </ul>
        )}
      </details>
    </section>
  );
};
