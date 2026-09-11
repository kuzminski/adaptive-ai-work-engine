"""Portable filesystem configuration for Adaptive AI Work Engine.

Defaults keep generated material inside this checkout.  Deployments that use
shared or pre-existing roots must opt in explicitly with the environment
variables below; no machine-specific installation path is assumed.
"""

from __future__ import annotations

import os
from pathlib import Path


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


AAW_ROOT = _path_from_env("AAW_ROOT", Path(__file__).resolve().parent)
EXTERNAL_ROOT = _path_from_env("AAW_EXTERNAL_ROOT", AAW_ROOT / "external")

# Runtime evidence is local by default and intentionally ignored by Git.
STATS_ROOT = _path_from_env("AAW_STATS_ROOT", AAW_ROOT / "output" / "03_STATS")
ROUTING_ROOT = _path_from_env("AAW_ROUTING_ROOT", AAW_ROOT / "output" / "routing")

# The original private playbook is optional.  Set AAW_PLAYBOOK_ROOT, or set
# the individual file overrides, when a deployment supplies those contracts.
PLAYBOOK_ROOT = _path_from_env("AAW_PLAYBOOK_ROOT", EXTERNAL_ROOT / "playbook")
MODEL_REGISTRY_PATH = _path_from_env(
    "AAW_MODEL_REGISTRY", AAW_ROOT / "MODEL_REGISTRY.json"
)
CLASSIFIER_PROMPT_PATH = _path_from_env(
    "AAW_CLASSIFIER_PROMPT", PLAYBOOK_ROOT / "execution" / "02_CHEAP_CLASSIFIER_V1.md"
)

CONTROL_CENTER_ROOT = AAW_ROOT / "CONTROL_CENTER"
CONTROL_CENTER_STATE = _path_from_env("AAW_CONTROL_CENTER_STATE", CONTROL_CENTER_ROOT / "STATE")
ANALYTICS_DB_PATH = _path_from_env(
    "AAW_ANALYTICS_DB", CONTROL_CENTER_ROOT / "ANALYTICS" / "aaw_analytics.sqlite"
)
