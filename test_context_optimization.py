#!/usr/bin/env python3
"""Tests for AAW Context Optimization (P1.3 Recovery Mandate).

Verifies that:
1. Work packets enforce strict, targeted file subsets rather than repository dumps.
2. Local preprocessing applies conservative compacting thresholds.
3. Critical scope, security boundaries, and definition_of_done are preserved.
4. Static sanity checks intercept trivial errors prior to reviewer invocations.
"""
from __future__ import annotations

from pathlib import Path

import local_preprocess as lp
import work_packet as wp


def test_work_packet_bounds_and_compact_scope() -> None:
    """Ensure weak/economic implementers are protected from context overload."""
    # When files to change exceed MAX_FILES_WEAK (6), it routes to STRONG
    oversized_plan = {
        "complexity": "NORMAL",
        "work_packet": {
            "files_to_read": ["file_0.py"],
            "files_to_change": [f"file_{i}.py" for i in range(8)],  # exceeds MAX_FILES_WEAK (6)
            "steps": [{"step_number": 1, "action": "inspect and change", "files": ["file_0.py"], "verify": "pytest"}],
            "verification_commands": [{"evidence_name": "pytest", "command": "pytest", "expect": "pass"}],
            "definition_of_done": ["All tests pass"],
            "pitfalls": ["Avoid mutating global state"],
            "out_of_scope": ["Refactoring callers"],
            "needs_strong_implementer": False,
            "strong_implementer_reason": "",
        },
    }
    assessment = wp.assess_difficulty(oversized_plan)
    assert assessment["route"] == wp.ROUTE_STRONG
    assert any("LARGE_ITERATION" in r for r in assessment["reasons"])


def test_work_packet_preserves_security_and_scope_invariants() -> None:
    """Definition of done, steps, and files to change are strictly required."""
    incomplete_plan = {
        "work_packet": {
            "files_to_read": ["main.py"],
            "files_to_change": [],
            "steps": [],
            "verification_commands": [],
            # Missing definition_of_done
        }
    }
    report = wp.lint_work_packet(incomplete_plan)
    assert "NO_STEPS" in report["issues"]
    assert "NO_FILES_TO_CHANGE" in report["issues"]
    assert "NO_VERIFICATION_COMMANDS" in report["issues"]
    assert "NO_DEFINITION_OF_DONE" in report["issues"]


def test_local_preprocessing_conservative_thresholds() -> None:
    """Preprocess thresholds must be within conservative bounds to save tokens."""
    thresholds = lp.DEFAULT_THRESHOLDS
    assert thresholds["task_chars"] <= 5000
    assert thresholds["handoff_chars"] <= 10000
    assert thresholds["diff_chars"] <= 20000
    assert thresholds["diff_max_chars"] <= 35000


def test_static_sanity_checks_intercept_defects_without_llm(tmp_path: Path) -> None:
    """Deterministic static sanity checks catch syntax/conflict errors for 0 tokens."""
    broken_py = tmp_path / "broken.py"
    broken_py.write_text("def broken_func(:\n    return 42\n", encoding="utf-8")

    conflict_py = tmp_path / "conflict.py"
    conflict_py.write_text("<<<<<<< HEAD\nfoo = 1\n=======\nfoo = 2\n>>>>>>> branch\n", encoding="utf-8")

    clean_py = tmp_path / "clean.py"
    clean_py.write_text("def clean_func():\n    return 42\n", encoding="utf-8")

    checks = wp.static_sanity_checks(tmp_path, ["broken.py", "conflict.py", "clean.py"], expect_changes=True)
    status_by_name = {c["name"]: c["status"] for c in checks}

    assert status_by_name.get("static::python_syntax") == "FAIL"
    assert status_by_name.get("static::conflict_markers") == "FAIL"
    assert status_by_name.get("static::changes_present") == "PASS"
