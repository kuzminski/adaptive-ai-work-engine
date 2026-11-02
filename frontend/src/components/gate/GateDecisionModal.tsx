/**
 * AAW Agentic Studio — Human Gate Decision Modal
 *
 * Accessible action dialog (role="dialog", aria-modal="true", Escape key dismissal)
 * supporting three operational mandates:
 * - ACCEPT: Final confirmation, summary of candidate files & branch promotion
 * - REJECT: Mandate rejection with feedback field explaining rationale
 * - CONTINUE: Continuation dialog with feedback field to steer next iteration
 *
 * Connects directly to engineStore.submitDecision(action, feedback)
 * and handles in-flight loading states and error notifications.
 */

import React, { useEffect, useState } from "react";
import {
  ShieldAlert,
  XCircle,
  Play,
  X,
  FileCode,
  GitBranch,
  AlertTriangle,
  Loader2,
  CheckCircle2,
} from "lucide-react";
import { DecisionAction, RunDetail } from "../../api/types";
import { useEngineStore } from "../../store/engineStore";

export interface GateDecisionModalProps {
  isOpen: boolean;
  mode: DecisionAction | null;
  run?: RunDetail | null;
  onClose: () => void;
  onSubmit?: (action: DecisionAction, feedback: string) => Promise<any>;
}

export const GateDecisionModal: React.FC<GateDecisionModalProps> = ({
  isOpen,
  mode,
  run,
  onClose,
  onSubmit,
}) => {
  const { submitDecision } = useEngineStore();

  const [feedback, setFeedback] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset form on modal open / mode change
  useEffect(() => {
    if (isOpen) {
      setFeedback("");
      setError(null);
      setSubmitting(false);
    }
  }, [isOpen, mode]);

  // Escape key listener for accessible dismissal
  useEffect(() => {
    if (typeof window === "undefined" || !isOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!submitting) {
          onClose();
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen, submitting, onClose]);

  if (!isOpen || !mode) {
    return null;
  }

  // ── Candidate & Workspace Metadata ─────────────────────────────────────────
  const branchName =
    run?.workspace?.branch ||
    run?.gate?.candidate?.branch ||
    "main";

  const candidateId =
    run?.gate?.candidate?.candidate_id ||
    run?.gate?.candidate?.head ||
    run?.run_id ||
    "HEAD";

  const changedFiles =
    run?.gate?.candidate?.changed_files ||
    (run as any)?.changed_files ||
    (run as any)?.dirty_files ||
    [];

  const filesCount = Array.isArray(changedFiles)
    ? changedFiles.length
    : typeof (run as any)?.modified_files_count === "number"
    ? (run as any).modified_files_count
    : 0;

  const acceptMeaning =
    run?.gate?.accept_meaning ||
    "Zatwierdzenie kandydata do integracji zewnętrznej. Zgodnie z inwariantem bezpieczeństwa AAW, silnik nie wykonuje automatycznego scalania (merge) ani wypychania (push) — pozostawia kod w gałęzi zadania do Twojej samodzielnej integracji.";

  // ── Submission Handler ─────────────────────────────────────────────────────
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!mode || submitting) return;

    const trimmed = feedback.trim();

    // Rejection requires rationale explanation
    if (mode === "REJECT" && !trimmed) {
      setError("Podanie powodu odrzucenia jest wymagane.");
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      if (onSubmit) {
        await onSubmit(mode, trimmed);
      } else {
        await submitDecision(mode, trimmed);
      }
      onClose();
    } catch (err: any) {
      setError(err?.message || "Wystąpił błąd podczas przesyłania decyzji.");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Mode-Specific Configurations ───────────────────────────────────────────
  let title = "";
  let modeBadge = "";
  let submitLabel = "";
  let submitIcon = <CheckCircle2 className="w-4 h-4" />;
  let submitBtnClass = "";
  let feedbackLabel = "";
  let feedbackPlaceholder = "";
  let feedbackRequired = false;

  if (mode === "ACCEPT") {
    title = "Zatwierdź wynik (READY FOR INTEGRATION)";
    modeBadge = "AKCEPTACJA";
    submitLabel = "Zatwierdź wynik (ACCEPT)";
    submitIcon = <CheckCircle2 className="w-4 h-4" />;
    submitBtnClass =
      "bg-emerald-600 hover:bg-emerald-500 active:bg-emerald-700 text-white shadow-emerald-950/50 border-emerald-400/50";
    feedbackLabel = "Notatka audytowa / komentarz (opcjonalnie):";
    feedbackPlaceholder =
      "Opcjonalny komentarz zatwierdzający, np. 'Zweryfikowano manualnie, spełnia kryteria...'";
    feedbackRequired = false;
  } else if (mode === "REJECT") {
    title = "Odrzuć zmiany i zakończ zadanie";
    modeBadge = "ODRZUCENIE";
    submitLabel = "Potwierdź odrzucenie (REJECT)";
    submitIcon = <XCircle className="w-4 h-4" />;
    submitBtnClass =
      "bg-rose-600 hover:bg-rose-500 active:bg-rose-700 text-white shadow-rose-950/50 border-rose-400/50";
    feedbackLabel = "Powód odrzucenia (wymagane):";
    feedbackPlaceholder =
      "Wyjaśnij powód odrzucenia (np. błędy semantyczne w implementacji, brak pokrycia brzegowych przypadków)...";
    feedbackRequired = true;
  } else if (mode === "CONTINUE") {
    title = "Kontynuuj orkiestrację z nowymi wytycznymi";
    modeBadge = "KONTYNUACJA";
    submitLabel = "Wznów i kontynuuj (CONTINUE)";
    submitIcon = <Play className="w-4 h-4" />;
    submitBtnClass =
      "bg-sky-600 hover:bg-sky-500 active:bg-sky-700 text-white shadow-sky-950/50 border-sky-400/50";
    feedbackLabel = "Wskazówki dla kolejnej iteracji:";
    feedbackPlaceholder =
      "Wprowadź dodatkowe instrukcje lub kryteria, którymi silnik ma się kierować w następnym cyklu...";
    feedbackRequired = false;
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="gate-modal-title"
      data-testid="gate-decision-modal"
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-fade-in"
    >
      {/* Backdrop */}
      <div
        className="fixed inset-0"
        data-testid="gate-modal-backdrop"
        onClick={() => {
          if (!submitting) onClose();
        }}
      />

      {/* Modal Card */}
      <div className="relative w-full max-w-xl overflow-hidden rounded-xl border border-subtle bg-surface shadow-2xl z-10 animate-scale-up">
        {/* Top colored accent bar based on action mode */}
        <div
          className={`h-1.5 w-full ${
            mode === "ACCEPT"
              ? "bg-emerald-500"
              : mode === "REJECT"
              ? "bg-rose-500"
              : "bg-sky-500"
          }`}
        />

        {/* Modal Header */}
        <div className="flex items-center justify-between p-5 border-b border-subtle bg-elevated/40">
          <div className="flex items-center gap-2.5">
            <span
              className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold tracking-wider uppercase border ${
                mode === "ACCEPT"
                  ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/30"
                  : mode === "REJECT"
                  ? "bg-rose-500/20 text-rose-300 border-rose-500/30"
                  : "bg-sky-500/20 text-sky-300 border-sky-500/30"
              }`}
            >
              {modeBadge}
            </span>
            <h3
              id="gate-modal-title"
              data-testid="gate-modal-title"
              className="text-base font-bold text-white tracking-tight"
            >
              {title}
            </h3>
          </div>

          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            data-testid="gate-modal-close-btn"
            className="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-elevated transition disabled:opacity-50"
            aria-label="Zamknij"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form Body */}
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {/* Summary Details for ACCEPT mode */}
          {mode === "ACCEPT" && (
            <div
              data-testid="accept-summary-box"
              className="rounded-lg bg-elevated/60 border border-subtle p-3.5 space-y-2.5 text-xs font-mono"
            >
              <div className="flex items-center justify-between text-slate-300">
                <span className="flex items-center gap-1.5 text-slate-400">
                  <GitBranch className="w-3.5 h-3.5 text-sky-400" />
                  Gałąź docelowa:
                </span>
                <span className="font-bold text-white">{branchName}</span>
              </div>

              <div className="flex items-center justify-between text-slate-300">
                <span className="flex items-center gap-1.5 text-slate-400">
                  <FileCode className="w-3.5 h-3.5 text-indigo-400" />
                  Kandydat:
                </span>
                <span className="font-bold text-slate-200 truncate max-w-[240px]">
                  {candidateId}
                </span>
              </div>

              <div className="flex items-center justify-between text-slate-300">
                <span className="text-slate-400">Zmodyfikowane pliki:</span>
                <span className="font-bold text-emerald-400">{`${filesCount} plików`}</span>
              </div>

              <div className="pt-2 border-t border-subtle/60 text-[11px] text-slate-400 font-sans">
                {acceptMeaning}
              </div>
            </div>
          )}

          {/* Explanation for REJECT mode */}
          {mode === "REJECT" && (
            <div
              data-testid="reject-warning-box"
              className="rounded-lg bg-rose-500/10 border border-rose-500/30 p-3.5 flex items-start gap-2.5 text-xs text-rose-300"
            >
              <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
              <div>
                <p className="font-semibold text-rose-200">Uwaga: Odrzucenie zmian</p>
                <p className="mt-0.5 text-rose-300/90 text-[11px] leading-relaxed">
                  Zadanie zostanie zatrzymane i oznaczone jako odrzucone przez człowieka. Zmiany nie
                  zostaną scalone z główną gałęzią repozytorium.
                </p>
              </div>
            </div>
          )}

          {/* Explanation for CONTINUE mode */}
          {mode === "CONTINUE" && (
            <div
              data-testid="continue-info-box"
              className="rounded-lg bg-sky-500/10 border border-sky-500/30 p-3.5 flex items-start gap-2.5 text-xs text-sky-300"
            >
              <ShieldAlert className="w-4 h-4 text-sky-400 shrink-0 mt-0.5" />
              <div>
                <p className="font-semibold text-sky-200">Kolejna iteracja orkiestracji</p>
                <p className="mt-0.5 text-sky-300/90 text-[11px] leading-relaxed">
                  Silnik przejdzie do kolejnego cyklu iteracji (Plan -&gt; Execute -&gt; Verify).
                  Wprowadź instrukcje, które pomogą agentowi poprawić lub rozszerzyć kod.
                </p>
              </div>
            </div>
          )}

          {/* Feedback Textarea Field */}
          <div className="space-y-1.5">
            <label
              htmlFor="gate-feedback-input"
              className="block text-xs font-semibold text-slate-300"
            >
              {feedbackLabel}
              {feedbackRequired && <span className="text-rose-400 ml-1">*</span>}
            </label>
            <textarea
              id="gate-feedback-input"
              data-testid="gate-feedback-input"
              rows={3}
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder={feedbackPlaceholder}
              disabled={submitting}
              required={feedbackRequired}
              className="w-full rounded-lg bg-elevated border border-subtle p-3 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-focus focus:ring-1 focus:ring-focus font-sans resize-none transition disabled:opacity-50"
            />
          </div>

          {/* Inline Error Message */}
          {error && (
            <div
              data-testid="gate-modal-error"
              className="p-2.5 rounded-lg bg-rose-500/15 border border-rose-500/30 text-rose-300 text-xs flex items-center gap-2"
            >
              <AlertTriangle className="w-3.5 h-3.5 text-rose-400 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {/* Action Buttons */}
          <div className="flex items-center justify-end gap-3 pt-2 border-t border-subtle">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              data-testid="gate-modal-cancel-btn"
              className="px-4 py-2 rounded-lg text-xs font-semibold text-slate-400 hover:text-white hover:bg-elevated border border-transparent hover:border-subtle transition disabled:opacity-50"
            >
              Anuluj
            </button>

            <button
              type="submit"
              disabled={submitting}
              data-testid="gate-modal-submit-btn"
              className={`px-4 py-2 rounded-lg text-xs font-bold border shadow-lg flex items-center gap-2 transition disabled:opacity-50 ${submitBtnClass}`}
            >
              {submitting ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>Przesyłanie decyzji...</span>
                </>
              ) : (
                <>
                  {submitIcon}
                  <span>{submitLabel}</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
