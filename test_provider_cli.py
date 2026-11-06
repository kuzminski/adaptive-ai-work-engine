"""Roadmap 0.6: one argv builder for iterations and workflow nodes; one parallelism ceiling."""
import json
from pathlib import Path

import adaptive_orchestrator as ao
import autonomy_adapters as adapters
import provider_cli as pc
import routing_contract as rc
import workflow_runner as wr

SCHEMA = {"type": "object"}
CODEX_HEAD = ["codex", "exec", "--ephemeral", "--skip-git-repo-check"]
CODEX_TAIL = ["--model", "m", "--config", 'model_reasoning_effort="high"', "--cd", "WT", "--output-schema", "S",
              "--output-last-message", "F", "--json", "-"]
CLAUDE_BASE = ["claude", "--print", "--no-session-persistence", "--permission-mode", "{perm}", "--model", "m",
               "--effort", "high", "--output-format", "json", "--json-schema", '{"type":"object"}', "--max-turns", "30"]


def _claude(perm):
    return [perm if part == "{perm}" else part for part in CLAUDE_BASE]


def test_builder_golden_argv():
    kw = dict(model="m", effort="high", worktree="WT", schema_path="S", final_path="F")
    assert pc.codex_argv("codex", read_only=False, **kw) == CODEX_HEAD + ["--sandbox", "workspace-write"] + CODEX_TAIL
    assert pc.codex_argv("codex", read_only=True, **kw) == CODEX_HEAD + ["--sandbox", "read-only"] + CODEX_TAIL
    assert pc.codex_argv("codex", read_only=False, bypass_sandbox=True, **kw) == \
        CODEX_HEAD + ["--dangerously-bypass-approvals-and-sandbox"] + CODEX_TAIL
    base = dict(model="m", effort="high", schema=SCHEMA, max_turns=30)
    assert pc.claude_argv("claude", read_only=True, **base) == _claude("plan")
    assert pc.claude_argv("claude", read_only=False, prompt="P", **base) == _claude("acceptEdits") + ["P"]
    guarded = pc.claude_argv("claude", read_only=False, guard_write_tools=True, **base)
    assert guarded[:len(CLAUDE_BASE)] == _claude("acceptEdits")
    assert "--allowedTools" in guarded and "Bash(git push:*)" in guarded


def test_iteration_adapter_uses_the_shared_builder_unchanged():
    rt = {"harness": "codex", "executable": "codex", "model": "m", "effort": "high"}
    argv, on_stdin = adapters._argv(rt, "execute", "WT", Path("S"), Path("F"), SCHEMA, 30)
    assert on_stdin and argv == CODEX_HEAD + ["--sandbox", "workspace-write"] + CODEX_TAIL
    rt.update(harness="claude", executable="claude")
    argv, on_stdin = adapters._argv(rt, "review", "WT", Path("S"), Path("F"), SCHEMA, 30)
    assert on_stdin and argv == _claude("plan")


def test_one_parallelism_ceiling():
    assert wr.MAX_PARALLEL_WORKERS_HARD_CEILING == rc.MAX_PARALLEL_WORKERS_HARD_CEILING == ao.MAX_PARALLEL_WORKERS
    assert wr.MAX_PARALLEL_WORKERS_DEFAULT == rc.MAX_PARALLEL_WORKERS_DEFAULT
