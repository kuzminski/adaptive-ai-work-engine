/**
 * AAW Agentic Studio — explicit roadmap diff for an edited continuation (roadmap 2.2).
 *
 * Shown in the wizard while the user edits a roadmap carried over from a previous run. The diff comes from the backend
 * (POST /api/roadmap/diff), so the same rules decide what is "added", "removed" or "reordered" as in the run's record.
 */

import React, { useEffect, useState } from "react";
import { GitCompare } from "lucide-react";
import { client } from "../../api/client";
import type { RoadmapDiff } from "../../api/types";

interface Props {
  previous: string[];
  current: string[];
}

export const RoadmapDiffCard: React.FC<Props> = ({ previous, current }) => {
  const [diff, setDiff] = useState<RoadmapDiff | null>(null);
  const [failed, setFailed] = useState(false);
  const key = JSON.stringify([previous, current]);

  useEffect(() => {
    let alive = true;
    const timer = setTimeout(() => {
      client
        .roadmapDiff(previous, current)
        .then((d) => alive && (setDiff(d), setFailed(false)))
        .catch(() => alive && setFailed(true));
    }, 250);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [key]);

  return (
    <div className="bg-elevated/40 border border-subtle rounded-lg p-3 space-y-2" data-testid="roadmap-diff-card">
      <div className="flex items-center gap-2 text-xs font-semibold text-white">
        <GitCompare className="w-3.5 h-3.5 text-sky-400" />
        <span>Zmiany względem poprzedniej roadmapy</span>
      </div>
      {failed && <div className="text-[11px] text-amber-300">Nie udało się porównać roadmap.</div>}
      {diff && (
        <div className="text-[11px] space-y-1">
          <div className="text-slate-300" data-testid="roadmap-diff-summary">{diff.summary}</div>
          {diff.added.map((p, i) => (
            <div key={`a${i}`} className="text-emerald-300">+ {p}</div>
          ))}
          {diff.removed.map((p, i) => (
            <div key={`r${i}`} className="text-rose-300 line-through">− {p}</div>
          ))}
          {diff.human_changed.map((p, i) => (
            <div key={`h${i}`} className="text-amber-300">≈ zmieniono [człowiek]: {p}</div>
          ))}
        </div>
      )}
    </div>
  );
};
