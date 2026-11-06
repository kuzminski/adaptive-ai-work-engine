#!/usr/bin/env python3
"""Single builder of direct-CLI argv for the codex and claude harnesses (roadmap 0.6).

Both execution paths (autonomy iterations: `autonomy_adapters`; workflow DAG: `workflow_runner.execute_llm_node`)
build their command here, so a flag is added or fixed in exactly one place. Behaviour per path is selected by
explicit parameters; nothing here starts a process.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

CLAUDE_WRITE_ALLOWED_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
CLAUDE_INTEGRATION_DENIED = ("Bash(git push:*)", "Bash(git merge:*)", "Bash(git rebase:*)", "Bash(git pull:*)",
                             "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git reset:*)")


def codex_argv(executable: str, *, model: str, effort: str, worktree: str, schema_path: Path | str,
               final_path: Path | str, read_only: bool, bypass_sandbox: bool = False) -> list[str]:
    """codex exec; prompt on stdin (`-`). `bypass_sandbox` replaces --sandbox (Windows workflow path, where the
    AppContainer sandbox denies temp directories; containment is enforced by the AAW runtime instead)."""
    argv = [str(executable), "exec", "--ephemeral", "--skip-git-repo-check"]
    if bypass_sandbox:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv.extend(["--sandbox", "read-only" if read_only else "workspace-write"])
    argv.extend(["--model", model, "--config", f'model_reasoning_effort="{effort}"',
                 "--cd", str(worktree), "--output-schema", str(schema_path),
                 "--output-last-message", str(final_path), "--json", "-"])
    return argv


def claude_argv(executable: str, *, model: str, effort: str, schema: Mapping[str, Any], max_turns: int,
                read_only: bool, guard_write_tools: bool = False, prompt: str | None = None) -> list[str]:
    """claude --print. With `prompt` the prompt is the final argument; without it the caller sends it on stdin
    (long handoffs exceed the Windows command-line limit). `guard_write_tools` adds the explicit tool allow/deny
    list for write roles (integration commands denied)."""
    argv = [str(executable), "--print", "--no-session-persistence",
            "--permission-mode", "plan" if read_only else "acceptEdits",
            "--model", model, "--effort", effort, "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")), "--max-turns", str(max_turns)]
    if guard_write_tools and not read_only:
        argv += ["--allowedTools", *CLAUDE_WRITE_ALLOWED_TOOLS, "--disallowedTools", *CLAUDE_INTEGRATION_DENIED]
    if prompt is not None:
        argv.append(prompt)
    return argv
