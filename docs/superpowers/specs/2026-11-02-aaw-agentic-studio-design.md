# AAW Agentic Studio (Command-Center) — Design Document

- **Date:** 2026-11-02
- **Author:** Antigravity & Human Partner
- **Status:** Final Proposed Design (Post-Builder Check)
- **Target Release:** AAW V0.4 / Product Studio
- **File Location:** `docs/superpowers/specs/2026-11-02-aaw-agentic-studio-design.md`

---

## 1. Intent and The Why

The Adaptive AI Work Engine (AAW) is a local-first, multi-agent AI orchestration engine designed for high reliability, deterministic verification, and human oversight. 

The current product user interface (`PRODUCT_UI`) is a functional but rudimentary 4-view web application (7-step wizard and static status tables). While operational, it does not convey the dynamic, parallel nature of modern agentic workflows, nor does it provide an ergonomic command-center experience for inspecting real-time agent topology, streaming logs, unified diffs, and critical Human-in-the-Loop decision gates.

The goal of **AAW Agentic Studio** is to transform the user experience into an attractive, hacker-grade, yet intuitive Command-Center (inspired by Linear, Cursor, Raycast, and LangGraph Studio) that gives developers and operators complete transparency, visual control, and confidence over autonomous AI coding tasks.

---

## 2. Goals, Non-Goals, and Anti-Goals

### Goals
1. **Command-Center Tri-Pane Ergonomics**: Provide a unified, responsive 3-pane layout:
   - **Left Rail**: Workspace navigation, Git status, AI provider health (Claude, Codex, Antigravity), autonomy policy presets, and Universal MCP Server configuration hub.
   - **Center Canvas**: Interactive execution graph faithfully rendering AAW's 7 engine phases (`PLAN`, `EXECUTE`, `SELF_VERIFY`, `REVIEW`, `REPAIR`, `FINAL_REVIEW`, `ROADMAP_CHECK`), active iteration state, and Human Gate decision points.
   - **Right Drawer**: Synchronized telemetry console (event-driven terminal logs filterable by role), unified diff inspector (latest & historical iterations), and verifiable Evidence Ledger.
2. **Deterministic Human Gate Controls**: Provide a high-contrast, prominent decision banner and modal when the engine reaches `AWAITING_HUMAN`, supporting verified actions (`Accept`, `Reject`, `Continue`) with keyboard shortcuts (`[A]`, `[R]`, `[C]`).
3. **Universal MCP 1-Click Setup**: Surface configuration status and 1-click install for Antigravity, Claude, and Codex via existing `/api/mcp/status` and `/api/mcp/configure` endpoints.
4. **Build-Time Isolation**: Keep the Python runtime 100% zero-dependency. Node.js/npm is used strictly during development and build time to produce static assets compiled into `PRODUCT_UI/`.

### Non-Goals
1. **No Backend Framework Migration**: Do not rewrite or replace `product_server.py`. The standard-library Python backend remains the sole runtime server and source of truth.
2. **No Cloud Telemetry or Tracking**: No third-party tracking scripts, external fonts, or analytics. The Studio runs 100% offline and loopback-only (`127.0.0.1`).
3. **No Automated Gate Bypass**: The UI must never offer an auto-approval bypass for human gates.

### Anti-Goals
1. **Bloated Runtime Bundle**: Avoid bundling hundreds of megabytes of unnecessary client libraries. Keep the compiled bundle compact (<1.5 MB total), fast-loading (<50ms), and clean.
2. **Breaking PyInstaller Packaging**: The build pipeline must compile cleanly so that `python packaging/build_portable.py` and `packaging/check_release_zip.py` work without manual intervention.

---

## 3. Constraints

1. **Security & Authentication**:
   - Loopback only (`127.0.0.1` / `localhost`).
   - Every API request must pass the `X-AAW-Token` header read from `meta[name="aaw-token"]` injected by `product_server.py`.
2. **Zero Runtime Python Dependencies**:
   - The Python backend continues using Python `http.server` and standard library modules only.
3. **Build Output Location**:
   - The frontend project lives in `frontend/` and builds into `PRODUCT_UI/` (index.html and static assets in `/static/`), served directly by `product_server.py`.
4. **Cross-Platform Compatibility**:
   - Must run seamlessly on Windows (PowerShell/cmd), Linux, and macOS.

---

## 4. Approaches Considered

### Approach A: Vanilla SPA Modernization (Zero-Dependency)
- **Concept:** Rebuild `PRODUCT_UI/app.js` and `PRODUCT_UI/app.css` using modern CSS variables, CSS Grid, and custom SVG node rendering without npm.
- **Trade-offs:** 100% zero-dependency even in dev, but requires writing complex custom graph layout and virtualized diff rendering from scratch, slowing down feature iteration.

### Approach B: Modern Bundle with Vite + React + Tailwind CSS (Selected)
- **Concept:** Isolate the frontend codebase into a standard `frontend/` directory with Vite, React, Tailwind CSS, Lucide Icons, and modern component primitives. The build step compiles to static HTML/CSS/JS in `PRODUCT_UI/`.
- **Trade-offs:** Introduces Node.js and npm into the developer build toolchain, but yields an exceptionally polished, maintainable, component-driven UI with rich icons, accessible modal dialogs, and robust reactive state management.
- **Why Selected:** Explicitly selected by the human partner to ensure a best-in-class, modern, aesthetic, and production-grade Agentic Studio UI.

---

## 5. Architectural Specification & How It Gets Made

### 5.1 Project Layout
```
c:\adaptive-ai-work-engine\
├── frontend/                     # Modern Frontend Source
│   ├── package.json              # Vite, React, Tailwind, Lucide
│   ├── vite.config.ts            # Builds into ../PRODUCT_UI with relative/static assets
│   ├── tailwind.config.js        # Design tokens & cyber-dark color system
│   ├── index.html                # Template with __AAW_TOKEN__ placeholder
│   └── src/
│       ├── main.tsx              # Application entry point
│       ├── tokens.css            # Cyber-dark theme & CSS custom properties
│       ├── api/                  # Typed client for product_server endpoints
│       ├── components/
│       │   ├── layout/           # Tri-pane layout (LeftRail, Canvas, RightDrawer)
│       │   ├── nav/              # Workspace, repo selector, provider pills
│       │   ├── graph/            # 7-phase engine execution graph & repair loop
│       │   ├── gate/             # Human Gate modal & sticky decision action bar
│       │   ├── terminal/         # Event-driven streaming log viewer with role filters
│       │   ├── diff/             # Unified file diff inspector
│       │   ├── evidence/         # Evidence ledger & test proof cards
│       │   └── mcp/              # Universal MCP status & 1-click installer modal
│       ├── pages/
│       │   ├── Home.tsx          # Welcome, quick-start, recent tasks
│       │   ├── Studio.tsx        # Command-Center live execution canvas
│       │   ├── Wizard.tsx        # New task configuration stepper
│       │   └── Settings.tsx      # Providers, models, presets, MCP configuration
│       └── store/                # UI and Run polling state management
├── PRODUCT_UI/                   # Compiled static distribution
│   ├── index.html                # Production entry point
│   ├── icon.svg
│   └── assets/                   # Bundled JS and CSS chunks
└── product_server.py             # Python HTTP server serving PRODUCT_UI/
```

### 5.2 Visual Design System: AAW Cyber-Dark
- **Backgrounds:**
  - `bg-base`: `#0b0d11` (Root application canvas)
  - `bg-surface`: `#13171f` (Panels, cards, rails)
  - `bg-elevated`: `#1a202c` (Modals, popovers, dropdowns)
  - `bg-terminal`: `#07080a` (Log console & code viewers)
- **Borders & Dividers:**
  - `border-subtle`: `#202736`
  - `border-focus`: `#3b82f6`
- **Engine Phase Accents:**
  - `PLAN`: `#818cf8` (Indigo)
  - `EXECUTE`: `#38bdf8` (Cyan)
  - `SELF_VERIFY`: `#c084fc` (Violet)
  - `REVIEW`: `#a78bfa` (Purple)
  - `REPAIR`: `#f43f5e` (Rose / Loopback)
  - `FINAL_REVIEW`: `#3b82f6` (Cobalt)
  - `ROADMAP_CHECK`: `#34d399` (Emerald)
- **Human Gate Accent:**
  - Gate Alert / Action: `#f59e0b` (Amber Gold, pulsing border)
- **Typography:**
  - Primary: `Inter, system-ui, sans-serif`
  - Monospace: `JetBrains Mono, Fira Code, monospace`

### 5.3 Tri-Pane Interaction Model
1. **Left Rail (~260px)**:
   - Header with AAW status and version pill.
   - Active Repository info & Git branch badge with working tree status.
   - Navigation links: Home, New Task, Runs, Settings.
   - Provider Health Card (Claude, Codex, Antigravity) with checkmarks.
   - Autonomy Policy Presets (`Conservative`, `Balanced`, `Full Autonomy`).
   - Universal MCP configuration badge with quick-setup modal trigger.
2. **Center Canvas (Flexible ~50-55%)**:
   - Task Header: Run ID, active goal, iteration count (`Iteration 2/5`), runtime elapsed.
   - 7-Phase Execution Graph: Dynamic SVG/Canvas node chain representing real AAW phase machine:
     `PLAN` → `EXECUTE` → `SELF_VERIFY` → `REVIEW` ⮂ `REPAIR` → `FINAL_REVIEW` → `ROADMAP_CHECK`.
   - Prominent Human Gate Banner: When state is `AWAITING_HUMAN`, pins a high-visibility review banner with summary, diff preview trigger, and `Accept`, `Reject`, `Continue` actions.
   - Artifacts & Task Briefs.
3. **Right Drawer (Flexible ~35-40%, collapsible)**:
   - Tab 1: **Terminal / Event Log** with role filter pills (`All`, `Planner`, `Implementer`, `Verifier`, `System`). Formatted from `autonomy_events` and timeline briefs.
   - Tab 2: **Unified Diff Inspector** highlighting additions/deletions for latest diff or selected execution iteration.
   - Tab 3: **Evidence Ledger** listing test suites, exit codes, and verification assertions.

### 5.4 Backend & Packaging Integration
- `product_server.py` static handler updated to support nested asset routes (e.g. `/static/assets/*`).
- `packaging/AAW.spec` data collection updated to recursively collect all files under `PRODUCT_UI`.
- `packaging/build_portable.py` runs `npm run build` inside `frontend/` if present before invoking PyInstaller.

---

## 6. Decisions & Builder Calls

The following architectural choices are formally settled:
1. **Graph Topology**: Renders the 7 actual engine phases (`PLAN`, `EXECUTE`, `SELF_VERIFY`, `REVIEW`, `REPAIR`, `FINAL_REVIEW`, `ROADMAP_CHECK`) with a loopback edge for `REPAIR` returning to verification, ensuring 100% accuracy with engine states.
2. **Human Gate Behavior**: Strictly adheres to the AAW engine contract (`AWAITING_HUMAN` state with `ACCEPT`, `REJECT`, `CONTINUE`), accompanied by the Evidence Ledger and Diff Inspector. No artificial pre-execution pause.
3. **Telemetry & Log Stream**: Constructed from `/api/runs/<id>` polling of journal events and timeline entries, formatted into an authentic, dark terminal view without modifying the Python subprocess runner.
4. **Autonomy Control**: Displays and configures Policy Presets (`Conservative`, `Balanced`, `Full Autonomy`) mapping to `product_presets.py`.
5. **Universal MCP Status**: Displays client readiness (Antigravity, Claude, Codex) from `/api/mcp/status` and provides a 1-click configuration modal.
6. **Diff Inspector**: Supports latest diff from `/api/runs/<id>/evidence` and historical execution diffs when selecting past iterations via `?execution_id=...`.

---

## 7. Approval & Next Steps

Upon human partner approval of this design document, we will proceed to invoke the `superpowers:writing-plans` skill to construct the detailed, step-by-step implementation plan.
