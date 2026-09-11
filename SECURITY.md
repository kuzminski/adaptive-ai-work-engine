# Security policy

AAW is an early-alpha local execution tool. Treat workflow specifications,
provider configuration, repositories, worktrees, and generated evidence as
operator-controlled inputs.

## Operating boundaries

- Review a workflow, binding, repository, and worktree before starting a run.
- Keep credentials in provider-supported authentication stores or environment
  configuration; never place them in workflows, queues, evidence, or commits.
- Treat `output/`, Control Center state and queues, SQLite indexes, journals,
  and generated reports as local data. They are intentionally ignored by Git.
- AAW does not authorize merging, pushing, pull-request creation, or automatic
  model fallback. Those require an explicit human action outside the runner.

## Reporting a vulnerability

Do not file a public issue with a secret, exploit trace, or private evidence.
Until a dedicated reporting channel is published, report the issue privately to
the repository owner with a minimal reproduction, affected version or commit,
and impact assessment.
