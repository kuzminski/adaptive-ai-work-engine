/**
 * AAW Agentic Studio — Settlement Card (Human Gate)
 *
 * Shown inside HumanGateBanner when `run.gate.settlement` exists (aaw_experience.settlement):
 * - what the run cost (USD when priced, otherwise proxy units)
 * - the saved range and the switches the benchmark supports (the backend's own text, never invented)
 * - per category (planning / implementation / verification / review) only if the payload carries it
 * - comparison with the forecast only when a forecast with a cost range is available
 */

import React from "react";
import { Coins, TrendingDown, TrendingUp, Layers } from "lucide-react";
import { fmtAmount, fmtUsd } from "../live/LiveChainPanel";

export interface SettlementCardProps {
  settlement: any;
  forecast?: any;
}

const CATEGORY_LABEL: Record<string, string> = {
  PLANNING: "Planowanie",
  IMPLEMENTATION: "Implementacja",
  VERIFICATION: "Weryfikacja",
  REVIEW: "Review",
};

const num = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

const pct = (ratio: number) => `${Math.round(ratio * 100)}%`;

export const SettlementCard: React.FC<SettlementCardProps> = ({ settlement, forecast }) => {
  if (!settlement || typeof settlement !== "object") {
    return null;
  }

  const unit: string = settlement.unit === "USD" ? "USD" : "proxy";
  const actual = num(settlement.actual) ? settlement.actual : null;
  const items: any[] = Array.isArray(settlement.items) ? settlement.items : [];
  const saving = settlement.saving && typeof settlement.saving === "object" ? settlement.saving : null;

  // Forecast comparison: only with a cost range in the same unit; otherwise say why it is missing.
  const estimate = forecast && forecast.estimate ? forecast.estimate : null;
  const forecastOk =
    estimate && estimate.cost && num(estimate.cost.mid) && estimate.unit === (unit === "USD" ? "USD" : "proxy");
  const deltaRatio = forecastOk && actual !== null && estimate.cost.mid > 0 ? (actual - estimate.cost.mid) / estimate.cost.mid : null;

  // Per-category breakdown, only when the payload carries it (keys normalised to upper case).
  const categories: [string, number][] =
    settlement.by_category && typeof settlement.by_category === "object"
      ? Object.entries(settlement.by_category)
          .filter(([, value]) => num(value))
          .map(([key, value]) => [key.toUpperCase(), value as number])
      : [];

  return (
    <div
      data-testid="settlement-card"
      className="mt-4 rounded-xl border border-amber-500/30 bg-slate-950/60 p-4 space-y-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-bold uppercase tracking-wider text-amber-300 flex items-center gap-1.5">
          <Coins className="w-3.5 h-3.5" />
          <span>Rozliczenie zadania</span>
        </h4>
        <span data-testid="settlement-unit" className="text-[10px] font-mono text-slate-400">
          jednostka: {unit}
        </span>
      </div>

      <div data-testid="settlement-actual" className="text-sm font-mono font-bold text-slate-100">
        {actual !== null ? `Koszt zadania: ${fmtAmount(actual, unit)}` : "Koszt zadania: brak danych"}
      </div>

      {/* Forecast vs actual */}
      <div data-testid="settlement-forecast" className="text-xs text-slate-300">
        {forecastOk && actual !== null ? (
          <div className="space-y-1">
            <div className="font-mono">
              Prognoza z podglądu na żywo: {fmtAmount(estimate.cost.low, unit)} – {fmtAmount(estimate.cost.high, unit)}
              {" "}(mediana {fmtAmount(estimate.cost.mid, unit)})
            </div>
            {deltaRatio !== null && (
              <div
                data-testid="settlement-delta"
                className={`flex items-center gap-1.5 font-semibold ${deltaRatio > 0 ? "text-rose-300" : "text-emerald-300"}`}
              >
                {deltaRatio > 0 ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                <span>
                  {deltaRatio > 0
                    ? `Przekroczono prognozę o ${pct(deltaRatio)}`
                    : `Mieściło się poniżej prognozy (o ${pct(-deltaRatio)})`}
                </span>
              </div>
            )}
          </div>
        ) : (
          <p data-testid="settlement-forecast-empty" className="text-slate-500">
            Brak prognozy do porównania — podgląd na żywo nie miał w chwili rozliczenia widełek kosztu.
          </p>
        )}
      </div>

      {/* Saving */}
      <div data-testid="settlement-saving" className="text-xs text-slate-300">
        {saving && num(saving.low) && num(saving.high) ? (
          <span className="text-emerald-300 font-semibold">
            Mogło kosztować mniej: {fmtAmount(saving.low, unit)} – {fmtAmount(saving.high, unit)}
          </span>
        ) : (
          <span className="text-slate-500">
            Brak oszczędności do pokazania — benchmark nie wskazuje lepszego wyboru z wystarczającą pewnością.
          </span>
        )}
      </div>

      {items.length > 0 && (
        <ul data-testid="settlement-items" className="space-y-1">
          {items.map((item, index) => (
            <li key={`item-${index}`} data-testid="settlement-item" className="text-[11px] font-mono text-slate-400">
              {String(item?.kind ?? "—")}: {String(item?.from ?? "—")} → {String(item?.to ?? "—")}
              {num(item?.saving_ratio) ? ` (${pct(item.saving_ratio)})` : ""}
            </li>
          ))}
        </ul>
      )}

      {categories.length > 0 && (
        <div data-testid="settlement-categories" className="space-y-1">
          <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400 flex items-center gap-1.5">
            <Layers className="w-3 h-3" />
            <span>Koszt wg kategorii</span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            {categories.map(([key, value]) => (
              <div key={key} data-testid={`settlement-category-${key.toLowerCase()}`} className="text-[11px] font-mono text-slate-300">
                {CATEGORY_LABEL[key] || key}: {unit === "USD" ? fmtUsd(value) : `${Math.round(value)} proxy`}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
