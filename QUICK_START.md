# AAW — Quick Start (Windows)

Release `0.3.0` · the app UI is in Polish; button names are quoted as they appear.

**Before you start (outside AAW, one time):**
- **Git for Windows** — <https://git-scm.com/downloads>
- **At least one AI CLI, installed and logged in with your own account:**
  - Claude CLI (Claude Code): install per <https://docs.anthropic.com/en/docs/claude-code/setup>, then run `claude` once in a terminal and log in;
  - or Codex CLI: install per <https://github.com/openai/codex>, then run `codex login`.

  Installing and logging in to these CLIs is **not part of AAW**. AAW only detects them and never stores passwords or keys.

**Steps (about 3–5 minutes):**

1. **Download** `AAW-Windows-x64.zip` from the latest release: <https://github.com/kuzminski/adaptive-ai-work-engine/releases/latest> (section *Assets*; not *Source code*).
2. **Unzip** it (right-click → *Extract All…* → *Extract*). No installation, no Python, no terminal needed. Do not start the app from inside the ZIP preview.
3. **Run** it: the extracted location contains one folder, `AAW`. Open it and double-click `AAW.exe` (Explorer may show it as `AAW`, type *Application*). Next to it: `SZYBKI_START.txt` (this guide in Polish), `README.md`, `EXAMPLES` and `_internal` (app files — do not modify). The app opens in your browser at `http://127.0.0.1:…` (local only).
4. **Windows warning:** the build is not code-signed yet, so SmartScreen may show *"Windows protected your PC"*. Click **More info → Run anyway**.
   If Microsoft Defender quarantines `AAW.exe` as `Trojan:Win32/Wacatac…!ml`, it is a generic heuristic false positive for unsigned PyInstaller apps: extract the ZIP outside OneDrive (e.g. `C:\AAW`), then in Windows Security → Protection history choose **Actions → Allow on device** (or exclude the `AAW` folder).
5. **Welcome screen** → **„Zaczynamy →"** (Let's start). A 7-step wizard opens.
6. **Project** — click **„Wybierz…"** (Choose…) and pick your normal project folder (a Git repository). AAW creates its own isolated copy; your folder is not changed. If the folder has uncommitted changes, AAW tells you exactly what to do (commit or stash).
7. **AI tools** — AAW detects Claude CLI / Codex CLI and shows FOUND / NOT FOUND, version and login status, with setup help if something is missing. Then **models**: keep the recommended levels (★) and optionally click **„Sprawdź modele"** (Verify models — one tiny request per model).
8. **Goal** — describe what you want in 1–3 sentences (or click an example). Optionally add a **first iteration** and a few **broad directions** (one per line — directions, not a detailed plan).
9. **START** — check the summary and press **START**.

**While it runs:** the run page shows the active phase (Plan → Implementation → Verification → Review → Repair → Final review → Next iteration), the model, elapsed time and roadmap progress.
- **STOP SAFELY** — stops after the current safe point (a step that changes files is finished first).
- **STOP NOW** — stops immediately; an interrupted file-changing step is never replayed blindly.
- **RESUME** — continues from the saved point (also after closing AAW or restarting the PC).

**At the end — Human Gate:** why AAW stopped, what was done, what remains, warnings, the changed files.
**„Akceptuj"** (Accept) = `READY_FOR_EXTERNAL_INTEGRATION`. AAW **never merges or pushes**: the result waits in the folder shown, for you to review and integrate.

Data, logs and runs: `%LOCALAPPDATA%\AAW`. Close the app: **„Ustawienia" → „Zamknij AAW"** (running tasks continue in the background).
