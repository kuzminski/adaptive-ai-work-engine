#!/usr/bin/env python3
"""AAW PRODUCT MVP V0.2 — per-user data location and product settings.

Everything the product writes lives under one user-writable folder
(`AAW_HOME`), never inside the portable application folder:

  Windows: %LOCALAPPDATA%\\AAW      other: ~/.local/share/aaw
  override: AAW_PRODUCT_HOME

  runs/          autonomy run roots (the engine's STATS_ROOT for product runs)
  worktrees/     isolated Git worktrees, one per run
  settings.json  product defaults (no credentials; AAW stores none)
  providers.json last provider detection (a cache; re-detect any time)
  model_probes.json  which exact model IDs the installed CLIs accepted/rejected (explicit "Verify models")
  MODEL_RECOMMENDATIONS.downloaded.json   optional online catalog update
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


def home() -> Path:
    override = os.environ.get("AAW_PRODUCT_HOME")
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "AAW"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "aaw"
    root.mkdir(parents=True, exist_ok=True)
    return root


def runs_root() -> Path:
    path = home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def worktrees_root() -> Path:
    path = home() / "worktrees"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(20):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except PermissionError:  # Windows: the file is being atomically replaced right now
            if attempt == 19:
                return default
            import time
            time.sleep(0.05)
        except (OSError, json.JSONDecodeError):
            return default
    return default


def write_json(path: Path, value: Any) -> None:
    """Atomic replace (temp file in the same folder)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")
        for attempt in range(20):
            try:
                os.replace(temp, path)
                break
            except PermissionError:  # Windows: a reader holds the file for a moment
                if attempt == 19:
                    raise
                import time
                time.sleep(0.05)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


DEFAULT_SETTINGS: dict[str, Any] = {
    "planning": "STRONGEST",
    "implementation": "RECOMMENDED",
    "review": "RECOMMENDED",
    "online_recommendation_updates": False,
    "max_repair_attempts": 2,
    "provider_timeout_s": 3600,
    "first_run_completed": False,
    # Opt-in controlled exploration (autonomy_explore): a small, visible share of ordinary iterations is implemented by
    # another already-runnable model of the same or lower cost class, so thin benchmark cells fill from real work.
    "exploration_enabled": False,
    "exploration_max_percent": 20,
    "exploration_max_per_run": 3,
}


def load_settings() -> dict[str, Any]:
    stored = read_json(home() / "settings.json", {}) or {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def save_settings(values: Mapping[str, Any]) -> dict[str, Any]:
    current = load_settings()
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS:
            continue
        expected = type(DEFAULT_SETTINGS[key])
        if expected is int and not (isinstance(value, int) and not isinstance(value, bool)):
            raise ValueError(f"{key} must be an integer")
        if expected is bool and not isinstance(value, bool):
            raise ValueError(f"{key} must be true/false")
        if expected is str and not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        current[key] = value
    if not 1 <= int(current["max_repair_attempts"]) <= 6:
        raise ValueError("max_repair_attempts must be between 1 and 6")
    if not 1 <= int(current["exploration_max_percent"]) <= 50:
        raise ValueError("exploration_max_percent must be between 1 and 50")
    if not 1 <= int(current["exploration_max_per_run"]) <= 10:
        raise ValueError("exploration_max_per_run must be between 1 and 10")
    write_json(home() / "settings.json", current)
    return current
