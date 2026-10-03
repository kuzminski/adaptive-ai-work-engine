#!/usr/bin/env python3
"""AAW product release identity (one place; shown in the app, `--version`, self-test and the ZIP)."""

RELEASE = "0.2.0-rc1"
RELEASE_NAME = "AAW Product MVP V0.2 — Release Candidate 1"
RELEASE_DATE = "2026-10-03"
ENGINE_BASE = "AAW_DEFAULT_AUTONOMOUS_POLICY_V0.3 (freeze 961e7f6)"


def describe() -> dict[str, str]:
    return {"release": RELEASE, "name": RELEASE_NAME, "date": RELEASE_DATE, "engine": ENGINE_BASE}
