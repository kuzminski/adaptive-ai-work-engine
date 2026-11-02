#!/usr/bin/env python3
"""AAW MCP Role Bridge — standard library bridge for MCP role sessions and validation.

Enables MCP clients and subprocesses running AAW roles (plan, execute, review,
repair, diagnose, self_verify, prepare_packet, final_review) to inspect structured
handoffs, query output schemas, and validate role outputs in-turn before committing
them to the autonomy controller or ledger.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import autonomy_adapters as aa


def get_role_handoff(handoff_path_or_dict: Path | dict[str, Any] | str) -> dict[str, Any]:
    """Retrieve and parse a structured role handoff from a file path, JSON string, or dict.

    Args:
        handoff_path_or_dict: A Path to a JSON file, a raw JSON string, or a dict.

    Returns:
        dict[str, Any]: Parsed role handoff dictionary.

    Raises:
        FileNotFoundError: If a file path is provided but does not exist.
        ValueError: If input is invalid JSON or does not resolve to a dictionary.
    """
    if isinstance(handoff_path_or_dict, dict):
        return dict(handoff_path_or_dict)

    if isinstance(handoff_path_or_dict, Path):
        path = handoff_path_or_dict
        if not path.is_file():
            raise FileNotFoundError(f"Handoff file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse JSON from {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Handoff JSON in {path} must be an object (dict), got {type(data).__name__}")
        return data

    if isinstance(handoff_path_or_dict, str):
        # Could be a file path or raw JSON
        trimmed = handoff_path_or_dict.strip()
        if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
            try:
                data = json.loads(trimmed)
            except Exception as exc:
                raise ValueError(f"Failed to parse JSON string: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"Handoff JSON must be an object (dict), got {type(data).__name__}")
            return data

        path = Path(handoff_path_or_dict)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"Failed to parse JSON from {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"Handoff JSON in {path} must be an object (dict), got {type(data).__name__}")
            return data
        raise FileNotFoundError(f"Handoff file not found: {handoff_path_or_dict}")

    raise ValueError(f"Unsupported handoff type: {type(handoff_path_or_dict).__name__}")


def _check_type(instance: Any, expected_type: str | Sequence[str]) -> bool:
    types = [expected_type] if isinstance(expected_type, str) else list(expected_type)
    for t in types:
        if t == "null" and instance is None:
            return True
        if t == "string" and isinstance(instance, str):
            return True
        if t == "boolean" and isinstance(instance, bool):
            return True
        if t == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if t == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            return True
        if t == "array" and isinstance(instance, list):
            return True
        if t == "object" and isinstance(instance, dict):
            return True
    return False


def _validate_schema(
    instance: Any,
    schema: dict[str, Any],
    path: str = "$",
    *,
    is_root_role: bool = False,
    role_name: str | None = None,
    strict: bool = False,
) -> tuple[bool, str | None]:
    # 1. Type validation
    if "type" in schema:
        expected = schema["type"]
        if not _check_type(instance, expected):
            got_type = "null" if instance is None else type(instance).__name__
            return False, f"Type mismatch at '{path}': expected {expected}, got {got_type}"

    # If instance is null and null was allowed, we stop here
    if instance is None:
        return True, None

    # 2. Enum validation
    if "enum" in schema:
        allowed = schema["enum"]
        if instance not in allowed:
            return False, f"Value {instance!r} at '{path}' is not one of enum {allowed}"

    # 3. Object validation
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        if strict:
            required_keys = schema.get("required", [])
        elif is_root_role and role_name:
            required_keys = aa.CORE_REQUIRED.get(role_name, schema.get("required", []))
        else:
            prop_keys = set(properties.keys())
            if prop_keys == {"name", "status", "summary", "command", "exit_code", "log_ref", "supersedes"}:
                required_keys = ["name", "status", "summary"]
            elif prop_keys == {"finding_key", "severity", "summary", "file", "blocking", "evidence_ref", "finding_code"}:
                required_keys = ["finding_key", "severity", "summary", "file", "blocking", "evidence_ref"]
            else:
                required_keys = schema.get("required", [])

        # Check required properties
        for req_key in required_keys:
            if req_key not in instance:
                return False, f"Missing required property '{req_key}' at '{path}'"

        # Check additionalProperties
        if schema.get("additionalProperties") is False:
            for k in instance:
                if k not in properties:
                    return False, f"Unexpected additional property '{k}' at '{path}'"

        # Validate properties
        for prop_key, prop_val in instance.items():
            if prop_key in properties:
                prop_schema = properties[prop_key]
                ok, err = _validate_schema(
                    prop_val,
                    prop_schema,
                    f"{path}.{prop_key}",
                    is_root_role=False,
                    strict=strict,
                )
                if not ok:
                    return False, err

    # 4. Array validation
    elif isinstance(instance, list):
        items_schema = schema.get("items")
        if items_schema and isinstance(items_schema, dict):
            for idx, item in enumerate(instance):
                ok, err = _validate_schema(
                    item,
                    items_schema,
                    f"{path}[{idx}]",
                    is_root_role=False,
                    strict=strict,
                )
                if not ok:
                    return False, err

    return True, None


def validate_role_output(
    role_name: str,
    output: dict[str, Any],
    *,
    strict: bool = False,
) -> tuple[bool, str | None]:
    """Validates an output dictionary against autonomy_adapters.OUTPUT_SCHEMAS[role_name].

    Args:
        role_name: Role name ('plan', 'execute', 'review', 'repair', 'diagnose', etc.)
        output: Dictionary representing candidate output from the role.
        strict: If True, requires all fields present in Codex-normalized schema.
                If False (default), uses CORE_REQUIRED for required fields while validating
                all provided fields against the full schema.

    Returns:
        tuple[bool, str | None]: (True, None) if valid, or (False, detailed error message).
    """
    if role_name not in aa.OUTPUT_SCHEMAS:
        known = sorted(aa.OUTPUT_SCHEMAS.keys())
        return False, f"Unknown role '{role_name}'. Available roles: {known}"

    if not isinstance(output, dict):
        return False, f"Role output must be a JSON object (dict), got {type(output).__name__}"

    schema = aa.OUTPUT_SCHEMAS[role_name]
    return _validate_schema(
        output,
        schema,
        path="$",
        is_root_role=True,
        role_name=role_name,
        strict=strict,
    )


class McpRoleSession:
    """Manages an in-turn AAW role session with schema validation feedback.

    Allows models and MCP clients to inspect structured role handoffs and
    receive instant schema validation feedback across multiple attempts.
    """

    def __init__(
        self,
        role_name: str,
        handoff: Path | dict[str, Any] | str,
        session_id: str | None = None,
        *,
        strict: bool = False,
    ) -> None:
        if role_name not in aa.OUTPUT_SCHEMAS:
            known = sorted(aa.OUTPUT_SCHEMAS.keys())
            raise ValueError(f"Unknown role '{role_name}'. Available roles: {known}")

        self.role_name = role_name
        self.session_id = session_id or str(uuid.uuid4())
        self.handoff = get_role_handoff(handoff)
        self.strict = strict
        self.submitted_output: dict[str, Any] | None = None
        self.validation_error: str | None = None
        self.is_valid: bool = False
        self.attempts: list[dict[str, Any]] = []

    def get_handoff(self) -> dict[str, Any]:
        """Returns the role handoff dictionary."""
        return self.handoff

    def get_schema(self) -> dict[str, Any]:
        """Returns the output JSON schema for this role."""
        return aa.OUTPUT_SCHEMAS[self.role_name]

    def get_instructions(self) -> str:
        """Returns standard AAW role instructions if available."""
        return aa.ROLE_INSTRUCTIONS.get(self.role_name, "")

    def get_core_required_keys(self) -> list[str]:
        """Returns minimal required keys for this role."""
        return aa.CORE_REQUIRED.get(self.role_name, [])

    def submit_output(self, output: dict[str, Any] | str) -> dict[str, Any]:
        """Submit candidate output, validate against role schema, and record attempt.

        Returns:
            dict containing validation results and feedback.
        """
        parsed: dict[str, Any]
        if isinstance(output, str):
            try:
                candidate = json.loads(output)
            except Exception as exc:
                err = f"JSON decode error: {exc}"
                self.validation_error = err
                self.is_valid = False
                res = {
                    "valid": False,
                    "error": err,
                    "role": self.role_name,
                    "attempts_count": len(self.attempts) + 1,
                    "output": None,
                }
                self.attempts.append(res)
                return res
            if not isinstance(candidate, dict):
                err = f"JSON root must be an object (dict), got {type(candidate).__name__}"
                self.validation_error = err
                self.is_valid = False
                res = {
                    "valid": False,
                    "error": err,
                    "role": self.role_name,
                    "attempts_count": len(self.attempts) + 1,
                    "output": None,
                }
                self.attempts.append(res)
                return res
            parsed = candidate
        elif isinstance(output, dict):
            parsed = output
        else:
            err = f"Expected dict or JSON string, got {type(output).__name__}"
            self.validation_error = err
            self.is_valid = False
            res = {
                "valid": False,
                "error": err,
                "role": self.role_name,
                "attempts_count": len(self.attempts) + 1,
                "output": None,
            }
            self.attempts.append(res)
            return res

        valid, error = validate_role_output(self.role_name, parsed, strict=self.strict)
        self.is_valid = valid
        self.validation_error = error
        if valid:
            self.submitted_output = parsed

        res = {
            "valid": valid,
            "error": error,
            "role": self.role_name,
            "attempts_count": len(self.attempts) + 1,
            "output": parsed if valid else None,
        }
        self.attempts.append(res)
        return res

    def save_output(self, path: Path | str) -> Path:
        """Persists the validated output to disk."""
        if not self.is_valid or self.submitted_output is None:
            raise ValueError(f"Cannot save: session has no valid submitted output ({self.validation_error})")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.submitted_output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target

    def status(self) -> dict[str, Any]:
        """Summary of current session state."""
        return {
            "session_id": self.session_id,
            "role": self.role_name,
            "is_valid": self.is_valid,
            "attempts_count": len(self.attempts),
            "last_error": self.validation_error,
        }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for role validation and inspection from external processes."""
    parser = argparse.ArgumentParser(prog="aaw_mcp_role_bridge", description=__doc__.split("\n\n")[0])
    parser.add_argument("--role", required=True, help="Role name (plan, execute, review, repair, diagnose)")
    parser.add_argument("--handoff", type=Path, help="Path to handoff JSON file")
    parser.add_argument("--schema", action="store_true", help="Print role output schema and exit")
    parser.add_argument("--validate", type=Path, help="Validate output JSON file against role schema")
    parser.add_argument("--strict", action="store_true", help="Enable strict schema validation")
    args = parser.parse_args(argv)

    if args.schema:
        if args.role not in aa.OUTPUT_SCHEMAS:
            print(json.dumps({"error": f"Unknown role '{args.role}'"}, indent=2))
            return 1
        print(json.dumps(aa.OUTPUT_SCHEMAS[args.role], indent=2))
        return 0

    if args.validate:
        if not args.validate.is_file():
            print(json.dumps({"valid": False, "error": f"File not found: {args.validate}"}, indent=2))
            return 1
        try:
            candidate = json.loads(args.validate.read_text(encoding="utf-8"))
        except Exception as exc:
            print(json.dumps({"valid": False, "error": f"JSON parse error: {exc}"}, indent=2))
            return 1
        valid, err = validate_role_output(args.role, candidate, strict=args.strict)
        print(json.dumps({"valid": valid, "error": err, "role": args.role}, indent=2))
        return 0 if valid else 1

    if args.handoff:
        try:
            session = McpRoleSession(args.role, args.handoff, strict=args.strict)
            print(json.dumps(session.status(), indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
