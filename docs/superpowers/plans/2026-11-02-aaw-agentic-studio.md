# AAW Agentic Studio (Command-Center) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and integrate the modern AAW Agentic Studio Command-Center UI (Option B: Vite + React + Tailwind + Lucide) into the Adaptive AI Work Engine, replacing the basic static web interface with a responsive tri-pane studio featuring real-time 7-phase engine graphs, prominent Human Gates, telemetry console, diff inspector, and 1-click Universal MCP setup.

**Architecture:** A standalone modern frontend application located in `frontend/` compiles to static HTML/CSS/JS in `PRODUCT_UI/` (served zero-dependency by `product_server.py`). The frontend connects via typed API clients passing `X-AAW-Token`, polling engine states, streaming journal timeline events, and sending human gate decisions (`Accept`, `Reject`, `Continue`).

**Tech Stack:** React 18+, TypeScript, Vite, Tailwind CSS, Lucide React, Python standard library `http.server`, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-11-02-aaw-agentic-studio-design.md`

---

## Global Constraints
- **Zero Runtime Dependencies for Python Backend:** `product_server.py` and `AAW.py` must require zero pip packages and zero node modules at runtime. Node.js/npm is strictly a development/build-time tool.
- **Local Loopback Security:** Loopback only (`127.0.0.1`). Every API request must pass the `X-AAW-Token` header read from `meta[name="aaw-token"]`.
- **Human Gate Safety Invariant:** Never allow automated approval of human decision gates (`ACCEPT`, `REJECT`).
- **Clean Windows / Linux / macOS Packaging:** The compiled frontend must bundle directly into `PRODUCT_UI/` and be packaged by `packaging/build_portable.py` and `packaging/AAW.spec`.

---

## Review Focus
1. **Token Ingestion Failure:** If `__AAW_TOKEN__` is not injected or `meta[name="aaw-token"]` is absent, API requests return HTTP 403 Forbidden. The client must throw a clear authentication error banner rather than a silent failure.
2. **Subprocess/Static Asset 404s:** Vite hashed asset files in `/static/assets/*` must be correctly resolved by `product_server.py` without traversing outside `PRODUCT_UI/`.
3. **Engine Phase Synchronization:** Engine states with `REPAIR` loops must transition correctly on the UI graph without getting stuck in a stale `active` animation.
4. **Large Diff Truncation:** Large diffs from `/api/runs/<id>/evidence` exceeding 200 KB must display a clean truncation notice without locking up the UI thread.
5. **Offline Operation:** The entire studio UI must render and function 100% offline (no external CDNs, Google Fonts, or remote asset fetches).

---

## Proposed Changes

### Task 1: Backend Static File Handler & Packaging Updates
**Files:**
- Modify: `product_server.py:270-282`
- Modify: `packaging/AAW.spec:38-42`
- Modify: `packaging/build_portable.py`
- Test: `tests/test_product_server_static.py`

- [ ] **Step 1: Write tests for recursive static routing and MIME type handling**
- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Update `product_server.py` to resolve nested files inside `PRODUCT_UI` and support `.js`, `.css`, `.woff2`, `.svg`**
- [ ] **Step 4: Update `packaging/AAW.spec` to recursively collect all files in `PRODUCT_UI`**
- [ ] **Step 5: Run tests and verify PASS**

---

### Task 2: Frontend Scaffolding, Vite Build Config & Design Tokens
**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tailwind.config.js`
- Create: `frontend/postcss.config.js`
- Create: `frontend/index.html`
- Create: `frontend/src/tokens.css`
- Create: `frontend/src/main.tsx`

- [ ] **Step 1: Create `frontend/package.json` with dependencies (React 18, Lucide React, Tailwind CSS, TypeScript, Vite)**
- [ ] **Step 2: Configure `vite.config.ts` with `base: "/static/"`, `outDir: "../PRODUCT_UI"`**
- [ ] **Step 3: Configure `tailwind.config.js` with AAW Cyber-Dark color tokens**
- [ ] **Step 4: Create `index.html` with `<meta name="aaw-token" content="__AAW_TOKEN__">`**
- [ ] **Step 5: Run `npm install` and verify build produces clean static files in `PRODUCT_UI`**

---

### Task 3: Typed API Client & Polling Engine Store
**Files:**
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/store/engineStore.ts`

- [ ] **Step 1: Define TypeScript interfaces in `types.ts`**
- [ ] **Step 2: Implement `client.ts` handling `X-AAW-Token` header and error reporting**
- [ ] **Step 3: Implement `engineStore.ts` polling `/api/runs/<id>`**
- [ ] **Step 4: Verify client builds without type errors**

---

### Task 4: Command Center Layout & Left Rail (Workspaces, Providers, MCP Hub)
**Files:**
- Create: `frontend/src/components/layout/StudioLayout.tsx`
- Create: `frontend/src/components/nav/LeftRail.tsx`
- Create: `frontend/src/components/mcp/MCPConfigModal.tsx`

- [ ] **Step 1: Implement `StudioLayout.tsx` with responsive tri-pane grid**
- [ ] **Step 2: Implement `LeftRail.tsx` with active repo details, git branch badge, AI providers health cards**
- [ ] **Step 3: Implement policy preset buttons (`Conservative`, `Balanced`, `Full Autonomy`)**
- [ ] **Step 4: Implement `MCPConfigModal.tsx` showing configuration status with 1-click setup execution**

---

### Task 5: 7-Phase Execution Graph with Animated State Machine
**Files:**
- Create: `frontend/src/components/graph/PhaseGraph.tsx`
- Create: `frontend/src/components/graph/PhaseNode.tsx`

- [ ] **Step 1: Implement `PhaseNode.tsx` displaying phase index, label, duration, and status icon**
- [ ] **Step 2: Implement `PhaseGraph.tsx` arranging the 7 phases in horizontal execution order**
- [ ] **Step 3: Render the `REPAIR` loopback curve connecting `REVIEW` back to `SELF_VERIFY`**
- [ ] **Step 4: Connect active node highlight to current engine step**

---

### Task 6: Sticky Human Gate Decision Banner & Action Modals
**Files:**
- Create: `frontend/src/components/gate/HumanGateBanner.tsx`
- Create: `frontend/src/components/gate/GateDecisionModal.tsx`

- [ ] **Step 1: Implement `HumanGateBanner.tsx` with amber gradient, pulsating border, and prominent reason statement**
- [ ] **Step 2: Add keyboard shortcut listeners (`A` = Accept, `R` = Reject, `C` = Continue)**
- [ ] **Step 3: Implement confirmation modal with input feedback field for `REJECT` and `CONTINUE` mandates**
- [ ] **Step 4: Wire action to `client.postDecision(runId, action, feedback)`**

---

### Task 7: Right Drawer (Telemetry Console, Unified Diff, Evidence Ledger)
**Files:**
- Create: `frontend/src/components/drawer/RightDrawer.tsx`
- Create: `frontend/src/components/terminal/TelemetryTerminal.tsx`
- Create: `frontend/src/components/diff/UnifiedDiffViewer.tsx`
- Create: `frontend/src/components/evidence/EvidenceLedger.tsx`

- [ ] **Step 1: Implement `TelemetryTerminal.tsx` formatting journal events with role badges and timestamp**
- [ ] **Step 2: Implement role filtering pills (`All`, `Planner`, `Implementer`, `Verifier`, `System`)**
- [ ] **Step 3: Implement `UnifiedDiffViewer.tsx` parsing unified diff text**
- [ ] **Step 4: Implement `EvidenceLedger.tsx` displaying verified test exit codes, modified files, and safety invariants**

---

### Task 8: Verification, Portable Packaging & Self-Test
**Files:**
- Modify: `packaging/build_portable.py`
- Test: Full build, release packaging check, Python self-test

- [ ] **Step 1: Update `packaging/build_portable.py` to trigger `npm run build` in `frontend/` before executing PyInstaller**
- [ ] **Step 2: Run `python AAW.py --self-test` to ensure engine, MCP, and UI pass all checks**
- [ ] **Step 3: Run `pytest -m "not slow"` to ensure zero Python regressions**
- [ ] **Step 4: Launch `python AAW.py` and verify Command Center renders cleanly in browser**
