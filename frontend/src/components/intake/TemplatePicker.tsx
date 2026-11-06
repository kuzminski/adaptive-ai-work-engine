/**
 * AAW Agentic Studio — project templates (roadmap 2.5).
 *
 * One click on a template copies goal, first iteration, roadmap, acceptance criteria and evidence into the wizard.
 * Everything stays editable; nothing starts here. Start from a template = pick, review, START (3 clicks).
 */

import React from "react";
import { LayoutTemplate } from "lucide-react";
import type { ProjectTemplate } from "../../api/types";
import type { IntakePrefill } from "./IdeaIntakePanel";

interface Props {
  templates: ProjectTemplate[];
  onApply: (prefill: IntakePrefill) => void;
}

export function templateToPrefill(t: ProjectTemplate): IntakePrefill {
  return {
    goal: t.goal,
    first_iteration: t.first_iteration,
    directions: t.directions,
    advanced: t.advanced,
  } as IntakePrefill;
}

export const TemplatePicker: React.FC<Props> = ({ templates, onApply }) => {
  if (!templates || templates.length === 0) return null;
  return (
    <div className="bg-surface border border-subtle rounded-xl p-4 space-y-3" data-testid="template-picker">
      <div className="flex items-center gap-2 text-sm font-semibold text-white">
        <LayoutTemplate className="w-4 h-4 text-sky-400" />
        <span>Szablon projektu</span>
        <span className="text-[11px] font-normal text-slate-400">wypełnia formularz, wszystko możesz zmienić</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {templates.map((t) => (
          <button
            key={t.id}
            type="button"
            data-testid={`template-${t.kind}`}
            onClick={() => onApply(templateToPrefill(t))}
            className="p-3 rounded-lg border border-subtle bg-elevated/40 text-left hover:border-sky-500/50 transition"
          >
            <div className="text-xs font-bold text-sky-300 mb-1">{t.title}</div>
            <div className="text-[11px] leading-tight text-slate-400">{t.description}</div>
          </button>
        ))}
      </div>
    </div>
  );
};
