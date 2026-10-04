# AAW — Adaptive AI Work Engine (portable, Windows x64)

AAW plans and carries out a series of small, reviewed iterations of work on
your Git project using the AI command-line tools you already have (Claude CLI
and/or Codex CLI), then stops and asks you to decide.

**Start here:** double-click `AAW.exe` in this folder. One-page guides:
`SZYBKI_START.txt` (Polish) and `QUICK_START.md` (English). Example tasks: `EXAMPLES\`.

## What you get

- A local app (`AAW.exe`) that opens in your browser on `127.0.0.1` only.
- A 7-step wizard: project → AI tools → models → goal → first iteration →
  direction → START.
- A live process view (active phase, model, elapsed time, roadmap progress),
  phase briefs with links to the raw evidence, STOP SAFELY / STOP NOW / RESUME.
- A Human Gate at the end: Accept, Add direction, Continue with new goal,
  Reject, View evidence.

## Safety guarantees

- Works in an **isolated copy** (Git worktree) of your project; your folder is
  never modified, cleaned or committed by AAW.
- **No automatic merge, no automatic push.** "Accept" means
  `READY_FOR_EXTERNAL_INTEGRATION` — you integrate the result yourself.
- **No silent model substitution.** Every model that will run is shown before
  START; an unavailable model stops the run at the Human Gate.
- An interrupted file-changing step is never replayed blindly.

## Requirements (installed separately, outside AAW)

- Windows 10/11 x64, Git for Windows.
- At least one AI CLI installed **and logged in**: Claude CLI (Claude Code) or
  Codex CLI. AAW stores no passwords, tokens or API keys.

## Files

| | |
|---|---|
| `AAW.exe` | the app (`--version`, `--self-test`, `--detect` for diagnostics) |
| `_internal\` | bundled runtime (do not modify) |
| `SZYBKI_START.txt`, `QUICK_START.md` | one-page start guide (Polish / English) |
| `README.md`, `CHANGELOG.md`, `VERSION.txt`, `LICENSE` | documentation |
| `EXAMPLES\` | 2–3 example tasks to paste into the wizard |

Your data (tasks, evidence, isolated copies, settings, logs) lives in
`%LOCALAPPDATA%\AAW`, never in the app folder. Deleting the app folder does not
delete your tasks.

## Known limitations of this release

- The executable is not code-signed (SmartScreen warning on first start).
- The app UI is in Polish.
- The Windows build is validated in CI with scripted (fake) AI CLIs; a real
  provider run on Windows has not been performed for this release (real runs
  were validated on Linux). See `CHANGELOG.md`.

License: Apache-2.0 (`LICENSE`).
