/**
 * AAW Agentic Studio — Experience View ("Doświadczenie i benchmark")
 *
 * Benchmark that builds itself from this machine's real runs (GET /api/experience):
 * - provenance strip: real data vs. labelled demo preview, number of iterations behind the numbers
 * - task-kind tabs and a cost-vs-solve-rate scatter plot (inline SVG, Pareto front, reliability)
 * - profile table for the selected kind (Wilson interval, cost per solved task, recommended star)
 * - recommendations from the backend and the horizon summary ("od pomysłu do końca w jednym ciągu")
 */

import React, { useEffect, useState } from "react";
import {
  Activity,
  AlertCircle,
  Database,
  FlaskConical,
  Lightbulb,
  RefreshCw,
  Star,
} from "lucide-react";
import { client } from "../../api/client";

type AnyRecord = Record<string, any>;

export interface ExperienceReportProps {
  data: AnyRecord;
  selectedKind?: string | null;
  onSelectKind?: (kind: string) => void;
}

const CONFIDENCE_LABEL: Record<string, string> = {
  RELIABLE: "wiarygodna",
  PRELIMINARY: "wstępna",
  TOO_FEW: "mało danych",
};

const RECOMMENDATION_TITLE: Record<string, string> = {
  SWITCH: "Zmiana modelu",
  KEEP: "Utrzymaj wybór",
  COLLECT: "Zbieranie danych",
  REVIEW_SHARE: "Koszt przeglądu",
  LONGER_CHAINS: "Dłuższe łańcuchy",
  SHORTER_CHAINS: "Krótsze łańcuchy",
};

const SIZE_LABEL: Record<string, string> = {
  "1-3": "1–3 iteracje",
  "4-8": "4–8 iteracji",
  "9+": "9 i więcej",
};

const W = 640;
const H = 340;
const PAD = { l: 66, r: 20, t: 16, b: 54 };

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

const pct = (v: unknown, digits = 0): string => (isNum(v) ? `${(v * 100).toFixed(digits)}%` : "—");

function fmtCost(v: unknown, unit: string): string {
  if (!isNum(v)) return "—";
  if (unit === "USD") return `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`;
  return `${Math.round(v)} j.`;
}

/** Task-kind blocks in the order the backend returns them (KINDS order), skipping malformed entries. */
function kindBlocks(bench: AnyRecord): AnyRecord[] {
  const kinds = bench?.kinds && typeof bench.kinds === "object" ? (bench.kinds as Record<string, AnyRecord>) : {};
  return Object.values(kinds).filter((b) => b && typeof b.kind === "string" && Array.isArray(b.points));
}

function sortByCost(points: AnyRecord[]): AnyRecord[] {
  return [...points].sort((a, b) => {
    const ca = isNum(a.cost_per_solved) ? a.cost_per_solved : Infinity;
    const cb = isNum(b.cost_per_solved) ? b.cost_per_solved : Infinity;
    return ca - cb;
  });
}

const EmptyNotice: React.FC<{ testId: string; text: string }> = ({ testId, text }) => (
  <p data-testid={testId} className="text-xs text-slate-400 bg-elevated border border-subtle rounded-lg p-3">
    {text}
  </p>
);

// -- scatter plot -----------------------------------------------------------------------------

const ScatterPlot: React.FC<{ points: AnyRecord[]; unit: string; kindLabel: string }> = ({ points, unit, kindLabel }) => {
  const plotted = points.filter((p) => isNum(p.cost_per_solved) && isNum(p.rate));
  const unplotted = points.length - plotted.length;

  if (plotted.length === 0) {
    return (
      <EmptyNotice
        testId="experience-scatter-empty"
        text={`${kindLabel}: żaden profil nie rozwiązał jeszcze zadania, więc nie ma czego zestawić na wykresie.`}
      />
    );
  }

  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const maxCost = Math.max(...plotted.map((p) => p.cost_per_solved as number));
  const xMax = maxCost > 0 ? maxCost * 1.12 : 1;
  const x = (v: number) => PAD.l + (v / xMax) * plotW;
  const y = (r: number) => PAD.t + (1 - Math.min(1, Math.max(0, r))) * plotH;

  const xTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * xMax);
  const yTicks = [0, 0.25, 0.5, 0.75, 1];
  const xLabel = unit === "USD"
    ? "Koszt na rozwiązane zadanie (USD, ceny katalogowe)"
    : "Koszt na rozwiązane zadanie (jednostki przybliżone)";
  const yLabel = "Skuteczność (% rozwiązanych iteracji)";

  const front = plotted
    .filter((p) => p.on_pareto)
    .sort((a, b) => (a.cost_per_solved as number) - (b.cost_per_solved as number));
  const frontLine = front.map((p) => `${x(p.cost_per_solved).toFixed(1)},${y(p.rate).toFixed(1)}`).join(" ");

  return (
    <div className="space-y-3">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-labelledby="experience-scatter-title"
        className="w-full h-auto"
        data-testid="experience-scatter-svg"
      >
        <title id="experience-scatter-title">
          {`Skuteczność względem kosztu na rozwiązane zadanie: ${kindLabel}`}
        </title>

        {yTicks.map((t) => (
          <g key={`y-${t}`}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke="#202736" strokeWidth={1} />
            <text x={PAD.l - 8} y={y(t) + 4} textAnchor="end" fontSize={10} fill="#94a3b8">
              {`${Math.round(t * 100)}%`}
            </text>
          </g>
        ))}

        {xTicks.map((t) => (
          <text key={`x-${t}`} x={x(t)} y={H - PAD.b + 16} textAnchor="middle" fontSize={10} fill="#94a3b8">
            {fmtCost(t, unit)}
          </text>
        ))}

        <line x1={PAD.l} x2={PAD.l} y1={PAD.t} y2={H - PAD.b} stroke="#475569" strokeWidth={1} />
        <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} stroke="#475569" strokeWidth={1} />

        <text x={PAD.l + plotW / 2} y={H - 8} textAnchor="middle" fontSize={11} fill="#cbd5e1">
          {xLabel}
        </text>
        <text
          transform={`translate(14 ${PAD.t + plotH / 2}) rotate(-90)`}
          textAnchor="middle"
          fontSize={11}
          fill="#cbd5e1"
        >
          {yLabel}
        </text>

        {front.length > 1 && (
          <polyline
            points={frontLine}
            fill="none"
            stroke="#38bdf8"
            strokeWidth={1.5}
            strokeDasharray="4 3"
            data-testid="experience-pareto-line"
          />
        )}

        {plotted.map((p) => {
          const cx = x(p.cost_per_solved as number);
          const cy = y(p.rate as number);
          const hollow = p.confidence === "TOO_FEW";
          const ci: number[] | null = Array.isArray(p.rate_ci95) && p.rate_ci95.length === 2 ? p.rate_ci95 : null;
          const tip = [
            `${p.label}`,
            `skuteczność ${pct(p.rate)}`,
            `n=${p.n}`,
            `koszt na rozwiązane ${fmtCost(p.cost_per_solved, unit)}`,
            p.on_pareto ? "na froncie Pareto" : null,
            hollow ? "mało danych" : null,
          ].filter(Boolean).join(", ");
          return (
            <g
              key={p.profile_id}
              data-testid={`experience-point-${p.profile_id}`}
              data-pareto={p.on_pareto ? "true" : "false"}
              data-confidence={p.confidence}
            >
              {ci && isNum(ci[0]) && isNum(ci[1]) && (
                <line x1={cx} x2={cx} y1={y(ci[1])} y2={y(ci[0])} stroke="#64748b" strokeWidth={1} />
              )}
              <circle
                cx={cx}
                cy={cy}
                r={p.is_best ? 8 : 6}
                fill={hollow ? "none" : p.on_pareto ? "#38bdf8" : "#64748b"}
                stroke={p.is_best ? "#fbbf24" : hollow ? "#94a3b8" : "#e2e8f0"}
                strokeWidth={p.is_best ? 2.5 : 1.5}
                strokeDasharray={hollow ? "3 2" : undefined}
              >
                <title>{tip}</title>
              </circle>
              <text x={cx + 11} y={cy + 4} fontSize={10} fill="#e2e8f0">
                {`${p.label} (n=${p.n})${hollow ? " · mało danych" : ""}`}
              </text>
            </g>
          );
        })}
      </svg>

      <ul className="flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-slate-400" data-testid="experience-legend">
        <li>● wypełniony punkt: na froncie Pareto</li>
        <li>◯ przerywany obrys: mało danych (poniżej progu wiarygodności)</li>
        <li>◉ złoty obrys: najlepszy wybór dla tej kategorii</li>
        <li>│ pionowa kreska: 95% przedział Wilsona</li>
        {unplotted > 0 && <li>{`${unplotted} profil(e) bez rozwiązanych zadań poza wykresem`}</li>}
      </ul>
    </div>
  );
};

// -- profile table ----------------------------------------------------------------------------

const ProfileTable: React.FC<{ points: AnyRecord[]; unit: string }> = ({ points, unit }) => (
  <div className="overflow-x-auto" data-testid="experience-profile-table">
    <table className="w-full text-xs text-left">
      <thead>
        <tr className="text-slate-400 border-b border-subtle">
          <th className="py-2 pr-3 font-medium">Profil</th>
          <th className="py-2 pr-3 font-medium">n</th>
          <th className="py-2 pr-3 font-medium">Skuteczność (95% przedział)</th>
          <th className="py-2 pr-3 font-medium">Koszt na rozwiązane</th>
          <th className="py-2 pr-3 font-medium">Wiarygodność</th>
        </tr>
      </thead>
      <tbody>
        {sortByCost(points).map((p) => {
          const ci: number[] | null = Array.isArray(p.rate_ci95) && p.rate_ci95.length === 2 ? p.rate_ci95 : null;
          return (
            <tr
              key={p.profile_id}
              data-testid={`experience-profile-row-${p.profile_id}`}
              className="border-b border-subtle/60 text-slate-200"
            >
              <td className="py-2 pr-3">
                <span className="inline-flex items-center gap-1.5">
                  {p.is_best && (
                    <Star
                      className="w-3.5 h-3.5 text-amber-400 fill-amber-400"
                      aria-label="Rekomendowany"
                      data-testid={`experience-best-star-${p.profile_id}`}
                    />
                  )}
                  <span>{p.label}</span>
                </span>
              </td>
              <td className="py-2 pr-3 font-mono">{isNum(p.n) ? p.n : "—"}</td>
              <td className="py-2 pr-3 font-mono">
                {pct(p.rate)}
                {ci && (
                  <span className="text-slate-500">{` (${pct(ci[0])}–${pct(ci[1])})`}</span>
                )}
              </td>
              <td className="py-2 pr-3 font-mono">{fmtCost(p.cost_per_solved, unit)}</td>
              <td className="py-2 pr-3">
                <span
                  className={
                    p.confidence === "RELIABLE"
                      ? "text-emerald-300"
                      : p.confidence === "PRELIMINARY"
                        ? "text-amber-300"
                        : "text-slate-500"
                  }
                >
                  {CONFIDENCE_LABEL[p.confidence] ?? "—"}
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  </div>
);

// -- recommendations and horizon --------------------------------------------------------------

const RecommendationCards: React.FC<{ recs: AnyRecord[]; kind: string }> = ({ recs, kind }) => {
  const relevant = recs.filter((r) => !r.kind || r.kind === kind);
  if (relevant.length === 0) {
    return (
      <EmptyNotice
        testId="experience-recommendations-empty"
        text="Dla tej kategorii nie ma jeszcze rekomendacji. Benchmark milczy tam, gdzie dane nie wystarczają."
      />
    );
  }
  return (
    <ul className="grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="experience-recommendations">
      {relevant.map((r, i) => (
        <li
          key={`${r.type}-${r.kind ?? "all"}-${i}`}
          data-testid={`experience-rec-${r.type}-${i}`}
          className="bg-elevated border border-subtle rounded-lg p-4 space-y-2"
        >
          <div className="flex items-center gap-2 text-xs font-semibold text-white">
            <Lightbulb className="w-3.5 h-3.5 text-sky-400 shrink-0" />
            <span>{RECOMMENDATION_TITLE[r.type] ?? String(r.type)}</span>
            <span className="ml-auto text-[10px] font-mono text-slate-500">{String(r.type)}</span>
          </div>
          <p className="text-xs text-slate-300 leading-relaxed">{typeof r.text === "string" ? r.text : ""}</p>
        </li>
      ))}
    </ul>
  );
};

const HorizonSummary: React.FC<{ horizon: AnyRecord }> = ({ horizon }) => {
  const text = typeof horizon?.text === "string" ? horizon.text : null;
  if (!horizon || !horizon.enough) {
    return (
      <div className="space-y-2" data-testid="experience-horizon-empty">
        {text ? (
          <p className="text-xs text-slate-400">{text}</p>
        ) : (
          <p className="text-xs text-slate-400">Za mało danych, by powiedzieć, jak daleko AAW dochodzi bez człowieka.</p>
        )}
      </div>
    );
  }
  const it = horizon.iterations ?? {};
  const bySize = horizon.by_size && typeof horizon.by_size === "object" ? (Object.entries(horizon.by_size) as [string, AnyRecord][]) : [];
  const stops = horizon.stop_reasons && typeof horizon.stop_reasons === "object" ? Object.entries(horizon.stop_reasons) : [];
  return (
    <div className="space-y-4">
      {text && <p className="text-xs text-slate-300 leading-relaxed" data-testid="experience-horizon-text">{text}</p>}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="bg-elevated border border-subtle rounded-lg p-3" data-testid="experience-horizon-finished">
          <div className="text-[11px] text-slate-400">Doszło do końca</div>
          <div className="text-lg font-mono text-emerald-300">{pct(horizon.finished_share)}</div>
        </div>
        <div className="bg-elevated border border-subtle rounded-lg p-3" data-testid="experience-horizon-median">
          <div className="text-[11px] text-slate-400">Mediana iteracji</div>
          <div className="text-lg font-mono text-white">{isNum(it.median) ? Math.round(it.median) : "—"}</div>
        </div>
        <div className="bg-elevated border border-subtle rounded-lg p-3" data-testid="experience-horizon-spread">
          <div className="text-[11px] text-slate-400">Rozrzut (p25–p75)</div>
          <div className="text-lg font-mono text-white">
            {isNum(it.p25) && isNum(it.p75) ? `${Math.round(it.p25)}–${Math.round(it.p75)}` : "—"}
          </div>
        </div>
        <div className="bg-elevated border border-subtle rounded-lg p-3" data-testid="experience-horizon-runs">
          <div className="text-[11px] text-slate-400">Zakończone zadania</div>
          <div className="text-lg font-mono text-white">{isNum(horizon.runs) ? horizon.runs : "—"}</div>
        </div>
      </div>
      {bySize.length > 0 && (
        <ul className="space-y-1 text-xs text-slate-300" data-testid="experience-horizon-by-size">
          {bySize.map(([size, v]: [string, AnyRecord]) => (
            <li key={size} data-testid={`experience-horizon-size-${size}`}>
              {`${SIZE_LABEL[size] ?? size}: ${pct(v?.finished_share)} doszło do końca (${isNum(v?.runs) ? v.runs : 0} zadań)`}
            </li>
          ))}
        </ul>
      )}
      {stops.length > 0 && (
        <p className="text-xs text-slate-400" data-testid="experience-horizon-stops">
          {`Powody zatrzymania: ${stops.map(([code, n]) => `${code} (${n})`).join(", ")}`}
        </p>
      )}
    </div>
  );
};

// -- report -----------------------------------------------------------------------------------

export const ExperienceReport: React.FC<ExperienceReportProps> = ({ data, selectedKind, onSelectKind }) => {
  const bench: AnyRecord = data?.benchmark ?? {};
  const unit = bench.unit === "USD" ? "USD" : "proxy";
  const blocks = kindBlocks(bench);
  const active: AnyRecord | null =
    blocks.find((b) => b.kind === selectedKind) ??
    [...blocks].sort((a, b) => (isNum(b.n) ? b.n : 0) - (isNum(a.n) ? a.n : 0))[0] ??
    null;
  const kindLabels: AnyRecord = data?.kinds && typeof data.kinds === "object" ? data.kinds : {};
  const isDemo = data?.demo === true;
  const iterations = isNum(bench.rows) ? bench.rows : 0;
  const horizon: AnyRecord = data?.horizon ?? {};
  const recs: AnyRecord[] = Array.isArray(data?.recommendations) ? data.recommendations : [];
  const overall: AnyRecord[] = Array.isArray(bench.overall) ? bench.overall : [];
  const overallBest = overall.find((r) => r.is_best);
  const warnings: string[] = Array.isArray(bench.warnings) ? bench.warnings.filter((w: unknown) => typeof w === "string") : [];
  const scopeLabel = bench.scope === "total" ? "koszt całkowity (z planowaniem i review)" : "koszt implementacji";

  return (
    <div className="space-y-6" data-testid="experience-report">
      {/* Provenance */}
      <section
        data-testid="experience-provenance"
        className={`rounded-xl p-5 shadow-lg space-y-3 border ${
          isDemo ? "bg-amber-500/10 border-amber-500/30" : "bg-surface border-subtle"
        }`}
      >
        <div className="flex flex-wrap items-center gap-3">
          {isDemo ? (
            <span
              data-testid="experience-demo-badge"
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-bold tracking-wide bg-amber-500/20 text-amber-200 border border-amber-500/40"
            >
              <FlaskConical className="w-3.5 h-3.5" />
              PRZYKŁAD / DEMO
            </span>
          ) : (
            <span
              data-testid="experience-real-badge"
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-bold tracking-wide bg-emerald-500/15 text-emerald-300 border border-emerald-500/30"
            >
              <Database className="w-3.5 h-3.5" />
              DANE RZECZYWISTE • TEN KOMPUTER
            </span>
          )}
          <span className="text-[11px] font-mono text-slate-400" data-testid="experience-scope">
            {scopeLabel}
          </span>
        </div>

        <p className="text-xs text-slate-400 leading-relaxed">
          {isDemo
            ? "Syntetyczny podgląd, nie Twoje dane. Pokazany tylko na wyraźne żądanie. Nie łączymy go z prawdziwymi uruchomieniami."
            : "Wszystkie liczby pochodzą wyłącznie z prawdziwych uruchomień tego komputera. Nic nie zostało zasymulowane."}
        </p>

        <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-300">
          <span data-testid="experience-iteration-count">
            <span className="font-mono text-white">{iterations}</span>
            {" rozstrzygniętych iteracji"}
          </span>
          <span data-testid="experience-run-count">
            <span className="font-mono text-white">{isNum(horizon.runs) ? horizon.runs : 0}</span>
            {" zakończonych zadań"}
          </span>
          <span data-testid="experience-kind-count">
            <span className="font-mono text-white">{blocks.length}</span>
            {" kategorii pracy"}
          </span>
        </div>

        {overallBest && (
          <p className="text-xs text-slate-300" data-testid="experience-overall-best">
            {`Najlepszy ogólnie dla Twojego miksu zadań: ${overallBest.label} (koszt na rozwiązane ${fmtCost(
              overallBest.cost_per_solved,
              unit,
            )}, skuteczność ${pct(overallBest.rate)}).`}
          </p>
        )}

        {warnings.length > 0 && (
          <ul className="space-y-1 text-[11px] text-amber-300" data-testid="experience-warnings">
            {warnings.map((w, i) => (
              <li key={i} className="flex items-start gap-1.5">
                <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-px" />
                <span>{w}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {!active ? (
        <section
          data-testid="experience-empty"
          className="bg-surface border border-subtle rounded-xl p-6 shadow-lg text-center space-y-2"
        >
          <Activity className="w-6 h-6 text-slate-500 mx-auto" />
          <h3 className="text-sm font-semibold text-white">Za mało danych</h3>
          <p className="text-xs text-slate-400 max-w-xl mx-auto leading-relaxed">
            Benchmark powstaje sam z prawdziwych uruchomień na tym komputerze. Każde rozstrzygnięte zadanie dokłada
            próbę. Dopóki ich nie ma, nie pokazujemy żadnych liczb zastępczych.
          </p>
        </section>
      ) : (
        <section className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-5" data-testid="experience-kind-panel">
          {/* Kind tabs */}
          <div role="tablist" aria-label="Kategoria pracy" className="flex flex-wrap gap-2" data-testid="experience-kind-tabs">
            {blocks.map((b) => {
              const isActive = b.kind === active.kind;
              return (
                <button
                  key={b.kind}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  data-testid={`experience-kind-${b.kind}`}
                  onClick={() => onSelectKind?.(b.kind)}
                  className={`px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                    isActive
                      ? "bg-sky-500/15 border-sky-500/50 text-sky-200"
                      : "bg-elevated border-subtle text-slate-400 hover:text-slate-200"
                  }`}
                >
                  <span>{kindLabels[b.kind] ?? b.label ?? b.kind}</span>
                  <span className="ml-2 font-mono text-[10px] text-slate-500">{`n=${isNum(b.n) ? b.n : 0}`}</span>
                </button>
              );
            })}
          </div>

          <div className="flex items-center gap-2 text-sm font-semibold text-white">
            <Activity className="w-4 h-4 text-sky-400" />
            <span>{`Koszt a skuteczność: ${kindLabels[active.kind] ?? active.label ?? active.kind}`}</span>
          </div>

          <ScatterPlot
            points={Array.isArray(active.points) ? active.points : []}
            unit={unit}
            kindLabel={kindLabels[active.kind] ?? active.label ?? active.kind}
          />

          <ProfileTable points={Array.isArray(active.points) ? active.points : []} unit={unit} />
        </section>
      )}

      {/* Recommendations */}
      <section className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4" data-testid="experience-recommendations-block">
        <div className="flex items-center gap-2 text-sm font-semibold text-white border-b border-subtle pb-3">
          <Lightbulb className="w-4 h-4 text-sky-400" />
          <span>Rekomendacje</span>
        </div>
        <RecommendationCards recs={recs} kind={active?.kind ?? ""} />
      </section>

      {/* Horizon */}
      <section className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4" data-testid="experience-horizon">
        <div className="flex items-center gap-2 text-sm font-semibold text-white border-b border-subtle pb-3">
          <Star className="w-4 h-4 text-sky-400" />
          <span>Horyzont: od pomysłu do końca w jednym ciągu</span>
        </div>
        <HorizonSummary horizon={horizon} />
      </section>
    </div>
  );
};

// -- container --------------------------------------------------------------------------------

export const ExperienceView: React.FC = () => {
  const [persona, setPersona] = useState<string>("");
  const [data, setData] = useState<AnyRecord | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedKind, setSelectedKind] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState<number>(0);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const res = await client.getExperience(persona ? { persona } : {});
        if (!cancelled) setData(res ?? null);
      } catch (err: any) {
        if (!cancelled) setError(err?.message || "Nie udało się pobrać benchmarku");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [persona, reloadToken]);

  const personas: Record<string, string> =
    data?.personas && typeof data.personas === "object" ? data.personas : {};

  return (
    <div className="max-w-5xl w-full mx-auto space-y-6 animate-fade-in pb-12" data-testid="experience-view">
      <div className="bg-surface border border-subtle rounded-xl p-5 shadow-xl flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-mono text-sky-400 uppercase mb-1">
            <Database className="w-3.5 h-3.5" />
            <span>Doświadczenie • benchmark z własnych uruchomień</span>
          </div>
          <h2 className="text-xl font-bold tracking-tight text-white">Doświadczenie i benchmark</h2>
          <p className="text-xs text-slate-400 mt-1 max-w-2xl">
            Benchmark buduje się sam z prawdziwych uruchomień na tym komputerze. Nic nie trzeba uruchamiać osobno.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-slate-400 flex items-center gap-2">
            <span>Źródło:</span>
            <select
              data-testid="experience-persona-select"
              value={persona}
              onChange={(e) => setPersona(e.target.value)}
              className="bg-elevated border border-subtle rounded-lg px-2 py-1.5 text-xs text-white focus:outline-none focus:border-sky-500"
            >
              <option value="">Dane rzeczywiste (ten komputer)</option>
              {Object.entries(personas).map(([key, label]) => (
                <option key={key} value={key}>{`Przykład: ${label} (demo)`}</option>
              ))}
            </select>
          </label>
          <button
            type="button"
            data-testid="experience-refresh-btn"
            onClick={() => setReloadToken((t) => t + 1)}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs bg-elevated border border-subtle text-slate-200 hover:text-white disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            <span>Odśwież</span>
          </button>
        </div>
      </div>

      {error && (
        <div
          data-testid="experience-error"
          className="p-4 bg-rose-500/10 border border-rose-500/30 rounded-xl flex items-center justify-between gap-3 text-rose-300 text-xs"
        >
          <span className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
            <span>{`Nie udało się pobrać benchmarku: ${error}`}</span>
          </span>
          <button
            type="button"
            data-testid="experience-retry-btn"
            onClick={() => setReloadToken((t) => t + 1)}
            className="px-3 py-1 rounded-lg bg-rose-500/20 border border-rose-500/40 text-rose-200 hover:text-white"
          >
            Spróbuj ponownie
          </button>
        </div>
      )}

      {loading && !data && (
        <div
          data-testid="experience-loading"
          className="bg-surface border border-subtle rounded-xl p-8 shadow-lg text-center text-xs text-slate-400"
        >
          Ładowanie benchmarku z uruchomień tego komputera…
        </div>
      )}

      {!loading && !error && !data && (
        <EmptyNotice testId="experience-no-data" text="Brak odpowiedzi z serwera. Odśwież widok, gdy backend będzie dostępny." />
      )}

      {data && <ExperienceReport data={data} selectedKind={selectedKind} onSelectKind={setSelectedKind} />}
    </div>
  );
};
