"""Unit and integration tests for aaw_mcp_role_bridge.py.

Validates:
1. `get_role_handoff` handling of dict, Path, JSON string, and file errors.
2. `validate_role_output` schema conformance across all AAW roles (plan, execute, review, repair, diagnose).
3. `McpRoleSession` interactive lifecycle, in-turn validation feedback, attempts history, and persistence.
4. CLI options (--schema, --validate, --handoff).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import aaw_mcp_role_bridge as rb
import autonomy_adapters as aa


def test_get_role_handoff_dict() -> None:
    data = {"mandate": "build universal mcp", "iteration": 1}
    assert rb.get_role_handoff(data) == data


def test_get_role_handoff_file(tmp_path: Path) -> None:
    file = tmp_path / "handoff.json"
    data = {"mandate": "build universal mcp", "iteration": 2}
    file.write_text(json.dumps(data), encoding="utf-8")

    assert rb.get_role_handoff(file) == data
    assert rb.get_role_handoff(str(file)) == data


def test_get_role_handoff_json_string() -> None:
    data = {"mandate": "from string", "status": "ok"}
    raw = json.dumps(data)
    assert rb.get_role_handoff(raw) == data


def test_get_role_handoff_not_found(tmp_path: Path) -> None:
    non_existent = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        rb.get_role_handoff(non_existent)


def test_get_role_handoff_invalid_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        rb.get_role_handoff(bad_file)


def test_validate_role_output_plan_valid() -> None:
    plan_out = {
        "status": "ITERATION",
        "mandate_hash": "hash_123",
        "goal": "implement universal mcp server",
        "roadmap_refs": ["STEP_1"],
        "scope_justification": "within bounds",
        "acceptance_criteria": ["self_test passes"],
        "touched_areas": ["engine"],
        "decisions": [{"kind": "ARCHITECTURE", "summary": "Use stdio and loopback HTTP"}],
        "skipped_items": [],
        "reason": None,
    }
    valid, err = rb.validate_role_output("plan", plan_out)
    assert valid is True
    assert err is None


def test_validate_role_output_execute_valid() -> None:
    exec_out = {
        "summary": "Implemented mcp bridge",
        "changed_files": ["aaw_mcp_role_bridge.py"],
        "checks": [
            {
                "name": "unit_tests",
                "status": "PASS",
                "summary": "All tests green",
            }
        ],
        "deviations": [],
        "uncertainties": [],
    }
    valid, err = rb.validate_role_output("execute", exec_out)
    assert valid is True
    assert err is None


def test_validate_role_output_review_valid() -> None:
    rev_out = {
        "verdict": "PASS",
        "summary": "All acceptance criteria verified",
        "findings": [],
    }
    valid, err = rb.validate_role_output("review", rev_out)
    assert valid is True
    assert err is None


def test_validate_role_output_review_with_finding() -> None:
    rev_out = {
        "verdict": "REPAIR_REQUIRED",
        "summary": "Issue detected in schema",
        "findings": [
            {
                "finding_key": "FINDING_1",
                "severity": "HIGH",
                "summary": "Null pointer risk",
                "file": "server.py",
                "blocking": True,
                "evidence_ref": "err.log",
            }
        ],
    }
    valid, err = rb.validate_role_output("review", rev_out)
    assert valid is True
    assert err is None


def test_validate_role_output_repair_valid() -> None:
    rep_out = {
        "summary": "Fixed null pointer",
        "addressed_findings": ["FINDING_1"],
        "changed_files": ["server.py"],
        "checks": [{"name": "regression", "status": "PASS", "summary": "Passed"}],
        "uncertainties": [],
    }
    valid, err = rb.validate_role_output("repair", rep_out)
    assert valid is True
    assert err is None


def test_validate_role_output_diagnose_valid() -> None:
    diag_out = {
        "summary": "Root cause identified",
        "diagnosis": {
            "root_cause": "Race condition",
            "why_prior_attempts_failed": "Insufficient timeout",
            "next_actions": ["Increase timeout"],
            "classification": "PRODUCT_DEFECT",
        },
    }
    valid, err = rb.validate_role_output("diagnose", diag_out)
    assert valid is True
    assert err is None


def test_validate_role_output_missing_key() -> None:
    invalid_plan = {
        "status": "ITERATION",
        # missing mandate_hash, goal, etc.
    }
    valid, err = rb.validate_role_output("plan", invalid_plan)
    assert valid is False
    assert "Missing required property" in (err or "")


def test_validate_role_output_invalid_enum() -> None:
    rev_out = {
        "verdict": "MAYBE",  # not in ['PASS', 'REPAIR_REQUIRED', 'ESCALATE']
        "summary": "maybe good",
        "findings": [],
    }
    valid, err = rb.validate_role_output("review", rev_out)
    assert valid is False
    assert "enum" in (err or "")


def test_validate_role_output_additional_properties_forbidden() -> None:
    rev_out = {
        "verdict": "PASS",
        "summary": "clean",
        "findings": [],
        "extra_field": "disallowed",
    }
    valid, err = rb.validate_role_output("review", rev_out)
    assert valid is False
    assert "Unexpected additional property 'extra_field'" in (err or "")


def test_validate_role_output_unknown_role() -> None:
    valid, err = rb.validate_role_output("nonexistent_role", {})
    assert valid is False
    assert "Unknown role" in (err or "")


def test_mcp_role_session_interactive_feedback(tmp_path: Path) -> None:
    session = rb.McpRoleSession("review", {"run_id": "RUN-100"})
    assert session.role_name == "review"
    assert session.get_schema() == aa.OUTPUT_SCHEMAS["review"]

    # Attempt 1: bad output
    res1 = session.submit_output({"verdict": "INVALID"})
    assert res1["valid"] is False
    assert session.is_valid is False
    assert len(session.attempts) == 1

    # Attempt 2: valid output
    res2 = session.submit_output({
        "verdict": "PASS",
        "summary": "Meets all criteria",
        "findings": [],
    })
    assert res2["valid"] is True
    assert session.is_valid is True
    assert len(session.attempts) == 2

    # Save output
    out_file = tmp_path / "final_review.json"
    session.save_output(out_file)
    assert out_file.is_file()
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved["verdict"] == "PASS"


def test_cli_interface(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Test --schema
    code = rb.main(["--role", "plan", "--schema"])
    assert code == 0
    captured = capsys.readouterr()
    schema_data = json.loads(captured.out)
    assert "properties" in schema_data

    # Test --validate with valid file
    valid_file = tmp_path / "valid_review.json"
    valid_file.write_text(json.dumps({
        "verdict": "PASS",
        "summary": "All good",
        "findings": [],
    }), encoding="utf-8")
    code = rb.main(["--role", "review", "--validate", str(valid_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert '"valid": true' in captured.out.lower()
