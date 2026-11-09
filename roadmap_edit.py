#!/usr/bin/env python3
"""AAW — explicit roadmap diff for an edited continuation (roadmap 2.2).

A run's mandate is frozen, so changing the roadmap between chains means a NEW run. This module only compares two
lists of roadmap points (old run vs. the edited list for the new run) so the change is shown to the user before START
and recorded with the new run. It never changes a mandate and has no I/O.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

_MARK = re.compile(r"^\s*\[(?:człowiek|czlowiek|human)\]\s*", re.IGNORECASE)


def is_human(point: str) -> bool:
    return bool(_MARK.match(point or ""))


def point_key(point: str) -> str:
    """Identity of a point: marker and spacing/case are not part of it (a marker change is reported separately)."""
    return " ".join(_MARK.sub("", point or "", count=1).split()).casefold()


def diff_roadmaps(old: Sequence[str], new: Sequence[str]) -> dict[str, Any]:
    old_keys, new_keys = [point_key(p) for p in old], [point_key(p) for p in new]
    old_set, new_set = set(old_keys), set(new_keys)
    added = [p for p, k in zip(new, new_keys) if k not in old_set]
    removed = [p for p, k in zip(old, old_keys) if k not in new_set]
    kept = [p for p, k in zip(new, new_keys) if k in old_set]
    common_old = [k for k in old_keys if k in new_set]
    common_new = [k for k in new_keys if k in old_set]
    old_human = {k: is_human(p) for p, k in zip(old, old_keys)}
    human_changed = [p for p, k in zip(new, new_keys) if k in old_human and old_human[k] != is_human(p)]
    reordered = common_old != common_new
    changed = bool(added or removed or human_changed or reordered)
    parts = []
    if added:
        parts.append(f"dodano {len(added)}")
    if removed:
        parts.append(f"usunięto {len(removed)}")
    if human_changed:
        parts.append(f"zmieniono oznaczenie [człowiek] w {len(human_changed)}")
    if reordered:
        parts.append("zmieniono kolejność")
    return {"added": added, "removed": removed, "kept": kept, "human_changed": human_changed,
            "reordered": reordered, "changed": changed,
            "summary": ", ".join(parts) if parts else "bez zmian względem poprzedniej roadmapy"}
