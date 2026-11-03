/**
 * AAW Agentic Studio — "Od pomysłu" (idea intake) panel.
 *
 * A collapsed-by-default block in the wizard. The user writes a loose idea; after an explicit confirmation
 * ONE read-only planner call turns it into scope, roadmap, risks and a forecast (POST /api/intake/propose).
 * Nothing starts here: "Wypełnij formularz" only copies the proposal into the wizard's fields.
 */

import React, { useState } from "react";
import {
  Lightbulb,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  CheckCircle2,
  RotateCw,
} from "lucide-react";
import { client } from "../../api/client";

export interface IntakeRisk {
  severity?: string;
  text?: string;
}

export interface IntakeRoadmapItem {
  title?: string;
  why?: string;
  kind?: string;
  size?: string;
  human_required?: boolean;
  human_reason?: string | null;
}

export interface IntakeProposal {
  title?: string;
  goal?: string;
  first_iteration?: string;
  roadmap?: IntakeRoadmapItem[];
  acceptance_criteria?: string[];
  constraints?: string[];
  forbidden_areas?: string[];
  required_evidence?: string[];
  assumptions?: string[];
  open_questions?: string[];
  risks?: IntakeRisk[];
  done_definition?: string;
}

export interface IntakeRange {
  low?: number;
  mid?: number;
  high?: number;
}

export interface IntakeForecast {
  total?: {
    iterations?: number;
    covered_items?: number;
    human_items?: number;
    unit?: string;
    cost?: IntakeRange | null;
    wall_s?: IntakeRange | null;
  } | null;
  chains?: { length?: number; count?: number; serious_reviews?: number } | null;
  gates?: Array<{ title?: string; reason?: string }>;
  reach?: { enough?: boolean; text?: string };
  open_ended?: boolean;
  warnings?: string[];
  note?: string;
}

/** What "Wypełnij formularz" copies into the wizard. Shape mirrors product_intake.to_prefill(). */
export interface IntakePrefill {
  goal: string;
  first_iteration: string;
  directions: string;
  advanced: {
    acceptance_criteria?: string;
    required_evidence?: string;
    forbidden_areas?: string;
    risks?: Array<{ description: string; severity: string; item_ids: string[]; source: string }>;
  };
}

export interface IntakeResultData {
  intake_id?: string;
  proposal?: IntakeProposal | null;
  prefill?: IntakePrefill | null;
  forecast?: IntakeForecast | null;
}

/** Money in the unit the user's own history was priced in (USD when every call was priced, else proxy units). */
export function formatCost(value: number, unit?: string): string {
  if (unit === "USD") {
    return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
  }
  return `${Math.round(value)} jedn. proxy`;
}

export function formatRange(range: IntakeRange | null | undefined, unit?: string): string | null {
  if (!range || typeof range.low !== "number" || typeof range.high !== "number") return null;
  return `${formatCost(range.low, unit)} – ${formatCost(range.high, unit)}`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

const SEVERITY_LABEL: Record<string, string> = { LOW: "niskie", MEDIUM: "średnie", HIGH: "wysokie" };
const SEVERITY_CLASS: Record<string, string> = {
  LOW: "bg-slate-500/20 text-slate-300 border-slate-500/40",
  MEDIUM: "bg-amber-500/10 text-amber-300 border-amber-500/40",
  HIGH: "bg-rose-500/10 text-rose-300 border-rose-500/40",
};

const ListBlock: React.FC<{ title: string; items?: string[]; testId: string }> = ({ title, items, testId }) => {
  const list = Array.isArray(items) ? items.filter((x) => typeof x === "string" && x.trim()) : [];
  if (list.length === 0) return null;
  return (
    <div className="space-y-1" data-testid={testId}>
      <div className="text-[11px] font-semibold text-slate-400 uppercase">{title}</div>
      <ul className="list-disc list-inside text-xs text-slate-300 space-y-0.5">
        {list.map((item, idx) => (
          <li key={idx}>{item}</li>
        ))}
      </ul>
    </div>
  );
};

/** Pure renderer of an intake result (also used by tests). */
export const IntakeResult: React.FC<{
  data: IntakeResultData;
  applied: boolean;
  onApply: () => void;
}> = ({ data, applied, onApply }) => {
  const proposal: IntakeProposal = data.proposal || {};
  const roadmap = Array.isArray(proposal.roadmap) ? proposal.roadmap : [];
  const risks = Array.isArray(proposal.risks) ? proposal.risks : [];
  const forecast = data.forecast || null;
  const total = forecast?.total || null;
  const costText = total ? formatRange(total.cost, total.unit) : null;
  const wallText = total?.wall_s && typeof total.wall_s.mid === "number" ? formatDuration(total.wall_s.mid) : null;
  const prefillRisks = data.prefill?.advanced?.risks?.length || 0;
  const prefillCriteria = (data.prefill?.advanced?.acceptance_criteria || "").split("\n").filter(Boolean).length;

  return (
    <div className="space-y-5" data-testid="intake-result">
      <section className="space-y-3" data-testid="intake-scope">
        <h4 className="text-xs font-semibold text-sky-300 uppercase">Zakres</h4>
        {proposal.goal ? (
          <p className="text-xs text-white leading-relaxed" data-testid="intake-goal">{proposal.goal}</p>
        ) : (
          <p className="text-xs text-slate-500" data-testid="intake-goal-empty">Planista nie podał celu.</p>
        )}
        {proposal.first_iteration && (
          <div className="text-xs text-slate-300" data-testid="intake-first-iteration">
            <span className="text-slate-500">Pierwsza iteracja: </span>
            {proposal.first_iteration}
          </div>
        )}
        {proposal.done_definition && (
          <div className="text-xs text-slate-300" data-testid="intake-done-definition">
            <span className="text-slate-500">Gotowe, gdy: </span>
            {proposal.done_definition}
          </div>
        )}
        <ListBlock title="Kryteria akceptacji" items={proposal.acceptance_criteria} testId="intake-acceptance" />
        <ListBlock title="Ograniczenia" items={proposal.constraints} testId="intake-constraints" />
        <ListBlock title="Obszary zakazane" items={proposal.forbidden_areas} testId="intake-forbidden" />
        <ListBlock title="Założenia planisty" items={proposal.assumptions} testId="intake-assumptions" />
        <ListBlock title="Otwarte pytania" items={proposal.open_questions} testId="intake-open-questions" />
      </section>

      <section className="space-y-2" data-testid="intake-roadmap-block">
        <h4 className="text-xs font-semibold text-sky-300 uppercase">Roadmapa</h4>
        {roadmap.length === 0 ? (
          <p className="text-xs text-slate-500" data-testid="intake-roadmap-empty">
            Brak punktów roadmapy poza pierwszą iteracją.
          </p>
        ) : (
          <ol className="space-y-1.5" data-testid="intake-roadmap">
            {roadmap.map((item, idx) => (
              <li key={idx} className="text-xs text-slate-200 flex flex-wrap gap-2" data-testid="intake-roadmap-item">
                <span className="font-mono text-slate-500">{idx + 1}.</span>
                <span>{item.title || "(bez tytułu)"}</span>
                {item.size && <span className="font-mono text-[10px] text-slate-400">[{item.size}]</span>}
                {item.human_required && (
                  <span className="text-amber-300" data-testid="intake-human-marker">
                    [człowiek]{item.human_reason ? ` ${item.human_reason}` : ""}
                  </span>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="space-y-2" data-testid="intake-risks">
        <h4 className="text-xs font-semibold text-sky-300 uppercase">Ryzyka</h4>
        {risks.length === 0 ? (
          <p className="text-xs text-slate-500" data-testid="intake-risks-empty">Planista nie wskazał ryzyk.</p>
        ) : (
          <ul className="space-y-1.5">
            {risks.map((risk, idx) => {
              const severity = typeof risk.severity === "string" ? risk.severity : "MEDIUM";
              return (
                <li key={idx} className="text-xs text-slate-200 flex items-start gap-2">
                  <span className={`shrink-0 px-1.5 py-0.5 rounded border text-[10px] font-mono ${SEVERITY_CLASS[severity] || SEVERITY_CLASS.MEDIUM}`}>
                    {SEVERITY_LABEL[severity] || severity}
                  </span>
                  <span>{risk.text}</span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="space-y-2" data-testid="intake-forecast">
        <h4 className="text-xs font-semibold text-sky-300 uppercase">Prognoza z Twojej historii</h4>
        {!forecast ? (
          <p className="text-xs text-slate-500" data-testid="intake-forecast-empty">Prognoza jest niedostępna.</p>
        ) : (
          <div className="space-y-1.5 text-xs text-slate-300">
            <p data-testid="intake-horizon">
              {forecast.reach?.text || "Za mało zakończonych zadań, by oszacować szansę dojścia do końca."}
            </p>
            <p data-testid="intake-iterations">
              Iteracje do wykonania: {total?.iterations ?? 0}
              {total?.human_items ? ` (+ ${total.human_items} punkt(ów) dla człowieka)` : ""}
            </p>
            <p data-testid="intake-cost">
              {costText
                ? `Koszt całego planu (p25–p75): ${costText}${wallText ? `, czas ok. ${wallText}` : ""}`
                : "Za mało danych z historii, by podać koszt całego planu."}
            </p>
            {forecast.chains && typeof forecast.chains.count === "number" && (
              <p data-testid="intake-chains">
                Serie: {forecast.chains.count} po {forecast.chains.length ?? "?"} iteracji, poważne review: {forecast.chains.serious_reviews ?? forecast.chains.count}
              </p>
            )}
            {Array.isArray(forecast.warnings) &&
              forecast.warnings.map((w, idx) => (
                <p key={idx} className="text-amber-300 flex items-start gap-1.5" data-testid="intake-warning">
                  <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
                  <span>{w}</span>
                </p>
              ))}
            {forecast.note && <p className="text-slate-500">{forecast.note}</p>}
          </div>
        )}
      </section>

      {data.prefill ? (
        <div className="flex flex-wrap items-center gap-3 pt-1" data-testid="intake-apply-block">
          <button
            type="button"
            onClick={onApply}
            className="px-4 py-2 bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/40 rounded-lg text-xs font-medium text-sky-300 transition"
            data-testid="intake-apply-btn"
          >
            Wypełnij formularz
          </button>
          {applied && (
            <p className="text-xs text-emerald-300 flex items-center gap-1.5" data-testid="intake-applied-note">
              <CheckCircle2 className="w-3.5 h-3.5" />
              <span>
                Formularz wypełniony
                {prefillCriteria ? `, kryteria akceptacji: ${prefillCriteria}` : ""}
                {prefillRisks ? `, ryzyka: ${prefillRisks}` : ""}. Nic jeszcze nie wystartowało.
              </span>
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
};

const CONFIRM_TEXT =
  "To jedno wywołanie planisty - kosztuje jak jeden krok planowania. Nic nie startuje.";
const MIN_IDEA_CHARS = 12;

export const IdeaIntakePanel: React.FC<{
  repoPath: string;
  chainMode: boolean;
  onApply: (prefill: IntakePrefill) => void;
  defaultExpanded?: boolean;
}> = ({ repoPath, chainMode, onApply, defaultExpanded = false }) => {
  const [expanded, setExpanded] = useState<boolean>(defaultExpanded);
  const [idea, setIdea] = useState<string>("");
  const [confirming, setConfirming] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<IntakeResultData | null>(null);
  const [applied, setApplied] = useState<boolean>(false);

  const ideaReady = idea.trim().length >= MIN_IDEA_CHARS;

  const runProposal = async () => {
    setConfirming(false);
    setLoading(true);
    setError(null);
    setApplied(false);
    try {
      const res = await client.postIntake({
        idea: idea.trim(),
        repo: repoPath.trim() || undefined,
        chain_mode: chainMode,
      });
      setResult(res as IntakeResultData);
    } catch (err: any) {
      setResult(null);
      setError(err?.message || "Nie udało się rozpisać pomysłu.");
    } finally {
      setLoading(false);
    }
  };

  const handleApply = () => {
    if (!result?.prefill) return;
    onApply(result.prefill);
    setApplied(true);
  };

  return (
    <div className="bg-surface border border-subtle rounded-xl p-5 shadow-lg space-y-4" data-testid="idea-intake-panel">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="w-full flex items-center justify-between text-left"
        data-testid="intake-toggle-btn"
      >
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <Lightbulb className="w-4 h-4 text-amber-400" />
          <span>Od pomysłu: rozpisz zakres i plan (opcjonalnie)</span>
        </div>
        {expanded ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
      </button>

      {expanded && (
        <div className="space-y-4 pt-2 border-t border-subtle">
          <div className="space-y-1">
            <label htmlFor="intake-idea-input" className="text-xs font-medium text-slate-300 block">
              Luźny pomysł (zdanie lub kilka akapitów):
            </label>
            <textarea
              id="intake-idea-input"
              rows={4}
              value={idea}
              onChange={(e) => {
                setIdea(e.target.value);
                setConfirming(false);
              }}
              placeholder="np. Chcę prostą aplikację, która przypomina klientom o wygasających subskrypcjach."
              className="w-full bg-elevated border border-subtle rounded-lg p-3 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-sky-500 leading-relaxed"
              data-testid="intake-idea-input"
            />
          </div>

          {!confirming && (
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setConfirming(true)}
                disabled={!ideaReady || loading}
                className="px-4 py-2 bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/40 rounded-lg text-xs font-medium text-sky-300 transition disabled:opacity-50"
                data-testid="intake-propose-btn"
              >
                Rozpisz pomysł
              </button>
              {!ideaReady && (
                <span className="text-[11px] text-slate-500">Opisz pomysł choć w kilku słowach (min. {MIN_IDEA_CHARS} znaków).</span>
              )}
            </div>
          )}

          {confirming && (
            <div className="p-3 rounded-lg border border-amber-500/30 bg-amber-500/10 space-y-3" data-testid="intake-confirm-box">
              <p className="text-xs text-amber-200">{CONFIRM_TEXT}</p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={runProposal}
                  className="px-3 py-1.5 bg-sky-600 hover:bg-sky-500 rounded-lg text-xs font-bold text-white transition"
                  data-testid="intake-confirm-btn"
                >
                  Potwierdzam, rozpisz
                </button>
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  className="px-3 py-1.5 bg-elevated hover:bg-slate-700 border border-subtle rounded-lg text-xs text-slate-300 transition"
                  data-testid="intake-cancel-btn"
                >
                  Anuluj
                </button>
              </div>
            </div>
          )}

          {loading && (
            <div className="flex items-center gap-2 text-xs text-slate-300" data-testid="intake-loading">
              <RotateCw className="w-3.5 h-3.5 animate-spin" />
              <span>Planista rozpisuje pomysł na zakres i roadmapę...</span>
            </div>
          )}

          {error && (
            <div className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-lg text-xs text-rose-300 flex items-center gap-2" data-testid="intake-error">
              <AlertTriangle className="w-4 h-4 shrink-0 text-rose-400" />
              <span>{error}</span>
            </div>
          )}

          {result && !loading && <IntakeResult data={result} applied={applied} onApply={handleApply} />}
        </div>
      )}
    </div>
  );
};
