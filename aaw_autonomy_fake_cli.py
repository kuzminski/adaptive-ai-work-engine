#!/usr/bin/env python3
"""Deterministic stand-in for the `claude` (or `agy`) CLI, for autonomy adapter tests only.

It is spawned by the *real* `autonomy_adapters.DirectRoleExecutor` path
(`workflow_runner.run_process(dispatch=True)`), so a test exercises the real
argv, stdin handoff, spawn receipt, ledger lifecycle and result parsing — only
the model is scripted. Never used by production code.

Scenario file (`AAW_FAKE_CLI_SCENARIO`): `{ "<ROLE>": [step, ...] }` where ROLE
is PLANNER | IMPLEMENTER | SELF-VERIFIER | REVIEWER | FINAL REVIEWER | REPAIRER.
The n-th call of a role uses step n (the last step repeats). A step may hold:

  output      structured result; "$MANDATE_HASH" and "$ITERATION_CRITERIA"
              are substituted from the handoff
  raw_text    non-JSON text instead of a structured result (invalid output)
  write_files {relative_path: content} written into the working directory
  exit_code   process exit code (default 0)
  session_id  provider session id to report (default: a fresh uuid4)
  sleep       seconds to sleep before answering
  fenced      (agy) no structured_output; the JSON answer is fenced inside prose in `response`

Invoked as `agy` (`--print` without Claude's `--no-session-persistence`), it reads the
role prompt from the file named in the `--print` text and answers with the Antigravity
print-mode JSON envelope `{"conversation_id", "status", "response", "usage", "structured_output"}`.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROLES = ("FINAL REVIEWER", "SELF-VERIFIER", "REVIEW-PRETREATMENT", "PLANNER", "IMPLEMENTER", "REVIEWER", "REPAIRER")


def _substitute(value, handoff):
    if isinstance(value, str):
        if value == "$MANDATE_HASH":
            return (handoff.get("MANDATE") or handoff.get("FROZEN_MANDATE") or {}).get("mandate_hash")
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            if item == "$ITERATION_CRITERIA":
                out.extend((handoff.get("ITERATION_CONTRACT") or {}).get("acceptance_criteria") or [])
            else:
                out.append(_substitute(item, handoff))
        return out
    if isinstance(value, dict):
        return {k: _substitute(v, handoff) for k, v in value.items()}
    return value


def main() -> int:
    scenario_path = Path(os.environ["AAW_FAKE_CLI_SCENARIO"])
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    argv = sys.argv[1:]
    agy = "--print" in argv and "--no-session-persistence" not in argv
    if agy:
        import re
        match = re.search(r"Read the file (.+?) completely", argv[argv.index("--print") + 1])
        prompt = Path(match.group(1)).read_text(encoding="utf-8") if match else ""
    else:
        prompt = sys.stdin.read()
    role = next((r for r in ROLES if f"You are the AAW {r}" in prompt or f"independent AAW {r}" in prompt), "UNKNOWN")
    body = prompt.split("HANDOFF:\n", 1)[1] if "HANDOFF:\n" in prompt else ""
    handoff = json.loads(body) if body.strip() else {}
    calls_path = scenario_path.with_name(scenario_path.name + ".calls.jsonl")
    previous = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()] \
        if calls_path.exists() else []
    count = sum(1 for c in previous if c["role"] == role)
    steps = scenario.get(role) or [{}]
    step = steps[min(count, len(steps) - 1)]
    session = step.get("session_id") or str(uuid.uuid4())
    with calls_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": role, "argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
                                 "session_id": session, "execution_id": handoff.get("EXECUTION_ID"),
                                 "iteration_id": handoff.get("ITERATION_ID"),
                                 "inherited_session_env": "CLAUDE_CODE_SESSION_ID" in os.environ,
                                 "handoff_keys": sorted(handoff)}) + "\n")
    if step.get("sleep"):
        time.sleep(float(step["sleep"]))
    for rel, content in (step.get("write_files") or {}).items():
        target = Path(os.getcwd()) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    code = int(step.get("exit_code", 0))
    if code:
        sys.stderr.write("fake provider failure\n")
        return code
    if agy:
        output = _substitute(step.get("output") or {}, handoff)
        envelope = {"conversation_id": session, "status": "success", "usage": {"input_tokens": 1, "output_tokens": 1}}
        if "raw_text" in step:
            envelope["response"] = step["raw_text"]
        elif step.get("fenced"):
            envelope["response"] = f"Here is the result:\n```json\n{json.dumps(output)}\n```\nDone."
        else:
            envelope.update(response=json.dumps(output), structured_output=output)
        sys.stdout.write(json.dumps(envelope))
        return 0
    envelope = {"session_id": session, "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0,
                "num_turns": 1, "is_error": False}
    if "raw_text" in step:
        envelope["result"] = step["raw_text"]
    else:
        envelope["structured_output"] = _substitute(step.get("output") or {}, handoff)
    sys.stdout.write(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
