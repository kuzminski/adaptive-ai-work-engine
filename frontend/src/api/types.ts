/**
 * AAW Agentic Studio — Backend & Engine Contract Types
 *
 * Faithfully mirrors AAW Python backend models and contracts:
 * - product_server.py
 * - product_view.py
 * - autonomy_contract.py
 * - product_providers.py
 * - aaw_mcp_config.py
 */

// ── Engine Lifecycles & Phases ─────────────────────────────────────────────

export type EnginePhase =
  | "PLAN"
  | "EXECUTE"
  | "SELF_VERIFY"
  | "REVIEW"
  | "REPAIR"
  | "FINAL_REVIEW"
  | "ROADMAP_CHECK";

export type PipelineNodeKey =
  | "PLAN"
  | "EXECUTE"
  | "SELF_VERIFY"
  | "REVIEW"
  | "REPAIR"
  | "FINAL_REVIEW"
  | "NEXT";

export type PhaseNodeState = "pending" | "active" | "stopped" | "failed" | "done";

export interface PhaseNode {
  key: PipelineNodeKey | string;
  label: string;
  state: PhaseNodeState;
  count: number;
}

export type EngineStatus =
  | "RUNNING"
  | "AWAITING_HUMAN"
  | "HUMAN_APPROVED"
  | "PROMOTED"
  | "REJECTED";

export type RunStatusCategory =
  | "RUNNING"
  | "STOPPING"
  | "PAUSED"
  | "INTERRUPTED"
  | "STARTING"
  | "START_FAILED"
  | "READY_FOR_DECISION"
  | "NEEDS_ATTENTION"
  | "ACCEPTED"
  | "REJECTED";

export type HomeSectionKey = "running" | "paused" | "attention" | "completed";

export type DecisionAction = "ACCEPT" | "REJECT" | "CONTINUE";

export type RoleFilter = "All" | "Planner" | "Implementer" | "Verifier" | "System";

export type DrawerTab = "terminal" | "diff" | "evidence";

// ── Providers & Models ─────────────────────────────────────────────────────

export type ProviderStatus = "FOUND" | "NOT_FOUND";
export type LoginStatus = "LOGGED_IN" | "NOT_LOGGED_IN" | "UNKNOWN";

export type AvailabilityState =
  | "AVAILABLE"
  | "NOT_VERIFIED"
  | "VERIFIED_HERE"
  | "NEEDS_CHECK"
  | "REJECTED_HERE"
  | "POLICY_UNAVAILABLE"
  | "CLI_NOT_FOUND"
  | "NOT_LOGGED_IN";

export interface ProviderProfile {
  profile_id: string;
  display_name: string;
  model: string;
  effort?: string | null;
  runnable: boolean;
  availability: AvailabilityState;
  availability_label: string;
  harness: string;
  runnable_here?: boolean;
  [key: string]: any;
}

export interface Provider {
  provider_id: "claude" | "codex" | "antigravity" | string;
  display_name: string;
  harness: string;
  status: ProviderStatus;
  executable: string | null;
  version: string | null;
  version_raw?: string | null;
  login: LoginStatus;
  login_detail: string | null;
  profiles: ProviderProfile[];
  runnable_profiles: string[];
  setup_help?: Record<string, any>;
}

export interface PolicyPreset {
  id: "conservative" | "balanced" | "autonomous" | string;
  name: string;
  description: string;
  autonomy_level: string;
  default_max_iterations?: number;
  [key: string]: any;
}

// ── MCP (Model Context Protocol) ───────────────────────────────────────────

export interface MCPClientStatus {
  path?: string;
  configured: boolean;
  desktop_path?: string;
  desktop_configured?: boolean;
  code_path?: string;
  code_configured?: boolean;
}

export interface MCPStatus {
  antigravity: MCPClientStatus;
  claude: MCPClientStatus;
  codex: MCPClientStatus;
}

export interface MCPConfigureRequest {
  target?: "all" | "antigravity" | "claude" | "codex" | string;
  install?: boolean;
}

export interface MCPConfigureResponse {
  target: string;
  installed: boolean;
  snippet?: string;
  config?: Record<string, any>;
  error?: string;
  [key: string]: any;
}

// ── Bootstrap Data ─────────────────────────────────────────────────────────

export interface BootstrapData {
  version: string;
  release: {
    release: string;
    commit?: string;
    dirty?: boolean;
    channel?: string;
    [key: string]: any;
  };
  settings: Record<string, any>;
  choices: Record<string, any>;
  providers: {
    detected_at?: string;
    providers: Provider[];
    [key: string]: any;
  } | null;
  notice: string;
  setup_notice: string;
  has_runs: boolean;
  recent_repos: string[];
  catalog: {
    version: string;
    source: string;
    last_updated?: string | null;
  };
  data_home: string;
  frozen: boolean;
  control_center_available: boolean;
  slot_labels: Record<string, string>;
  presets: PolicyPreset[];
  templates?: ProjectTemplate[];
  limits: {
    max_field_chars: number;
    max_roadmap_chars: number;
    max_directions: number;
    default_max_iterations: number;
    hard_max_iterations: number;
    hard_max_repair_attempts: number;
  };
}

// ── Run Lifecycle & Process Views ──────────────────────────────────────────

export interface RunControlState {
  stop: boolean;
  force_stop: boolean;
  resume: boolean;
  lock_token: string | null;
}

export interface RoadmapItem {
  item_id: string;
  title: string;
  status: string;
  recurring: boolean;
  reason?: string | null;
}

export interface ProcessView {
  iteration: number;
  iteration_id: string | null;
  nodes: PhaseNode[];
  phase: EnginePhase | string | null;
  phase_label: string | null;
  phase_elapsed_s: number | null;
  activity: string | null;
  model: string | null;
  role: string | null;
  execution_id: string | null;
  step_elapsed_s: number | null;
  run_elapsed_s: number | null;
  roadmap: { done: number; total: number };
  standing?: {
    item_id: string;
    status: string;
    iterations: number;
    reason?: string | null;
  } | null;
  roadmap_items: RoadmapItem[];
  iterations_done: number;
  max_iterations: number;
  eta: string | null;
}

export interface BriefCheck {
  name: string;
  status: "PASS" | "FAIL" | "ERROR" | string;
  summary?: string | null;
}

export interface IterationBrief {
  kind: string;
  phase: string;
  model: string;
  goal: string | null;
  done: string[];
  checks: BriefCheck[];
  problems: string[];
  handed_over: string[];
  at: string | null;
  execution_id: string | null;
  ledger_state: string | null;
  close_reason: string | null;
  evidence_refs: string[];
}

export interface TimelineRow {
  index: number | null;
  iteration_id: string | null;
  label: string;
  goal: string | null;
  roadmap_refs: string[];
  started_at: string | null;
  finished_at: string | null;
  briefs: IterationBrief[];
}

export interface HumanGateCandidate {
  candidate_id: string | null;
  head: string | null;
  diff_sha256: string | null;
  changed_files: string[];
  last_repair?: any;
  branch: string | null;
  worktree: string | null;
  base_commit: string | null;
  promotable: boolean;
  roadmap_exhausted: boolean;
}

export interface HumanGateActions {
  accept: boolean;
  accept_needs_early_end: boolean;
  add_direction: boolean;
  new_goal: boolean;
  reject: boolean;
  evidence: boolean;
}

export interface HumanGate {
  why: string;
  hold_reason: string;
  escalation_code: string | null;
  done: string[];
  remaining: string[];
  warnings: string[];
  planner_end_reason: string | null;
  could_continue: boolean;
  candidate: HumanGateCandidate;
  actions: HumanGateActions;
  decision: any;
  human: any;
  promotion: any;
  accept_meaning: string;
  merge_push: string;
  main_merge_allowed: boolean;
}

export interface RunDetail {
  run_id: string;
  status: RunStatusCategory;
  status_label: string;
  section: string;
  project?: string;
  goal?: string;
  created_at?: string;
  workspace?: {
    repo_path?: string;
    project_name?: string;
    branch?: string;
    base_commit?: string;
    worktree?: string;
    [key: string]: any;
  };
  resolution?: Record<string, any>;
  controls: RunControlState;
  status_detail?: string | null;
  stop_request?: any;
  stop_effect?: any;
  worker_exit?: any;
  direction?: {
    goal?: string;
    first_iteration?: string;
    directions_text?: string;
    frozen: boolean;
  };
  working_roadmap?: any;
  working_roadmap_history?: any[];
  process: ProcessView | null;
  timeline: TimelineRow[];
  gate: HumanGate | null;
  last_activity?: string | null;
  engine?: {
    status?: EngineStatus | string;
    phase?: EnginePhase | string;
    policy_preset?: string;
    contract?: string;
  };
}

export interface RunSummaryCard {
  run_id: string;
  project?: string;
  goal?: string;
  short_goal?: string;
  status: RunStatusCategory;
  can_resume?: boolean;
  lock_token?: string | null;
  status_label?: string;
  iteration?: number;
  phase?: string | null;
  activity?: string | null;
  last_activity?: string | null;
  roadmap?: { done: number; total: number } | null;
}

export interface RunsResponse {
  sections: {
    running: RunSummaryCard[];
    paused: RunSummaryCard[];
    attention: RunSummaryCard[];
    completed: RunSummaryCard[];
  };
}

// ── Evidence & Telemetry ───────────────────────────────────────────────────

export interface AutonomyJournalEvent {
  event_type: string;
  phase?: string;
  iteration_id?: string;
  occurred_at?: string;
  payload?: Record<string, any>;
  [key: string]: any;
}

export interface ExecutionSummary {
  execution_id: string;
  phase?: string;
  role?: string;
  iteration_id?: string;
  profile?: string;
  model?: string;
  effort?: string;
  selection?: any;
  [key: string]: any;
}

export interface EvidenceView {
  run_dir?: string;
  state_path?: string;
  journal?: AutonomyJournalEvent[];
  executions?: ExecutionSummary[];
  mandate?: Record<string, any>;
  directional_charter?: Record<string, any>;
  latest_diff?: string | null;
  task?: Record<string, any>;
  // Present when querying a specific execution_id:
  execution_id?: string;
  descriptor?: Record<string, any>;
  ledger?: Record<string, any>;
  result_artifact?: Record<string, any>;
  journal_events?: AutonomyJournalEvent[];
}

// ── Task Creation & Repository Inspection ──────────────────────────────────

export interface RepoInspectResult {
  path: string;
  is_git: boolean;
  current_branch?: string | null;
  dirty?: boolean;
  dirty_files?: string[];
  head_commit?: string | null;
  error?: string | null;
}

export interface StartTaskPayload {
  repo: string;
  goal: string;
  first_iteration?: string;
  preset?: string;
  directions?: string[];
  choices?: Record<string, string>;
  max_iterations?: number;
  [key: string]: any;
}

export interface TaskStartResult {
  run_id: string;
  status: string;
  [key: string]: any;
}

// ── Generic API Error ──────────────────────────────────────────────────────

export class ApiError extends Error {
  status: number;
  details?: any;

  constructor(message: string, status: number = 500, details?: any) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

// ── Project templates (roadmap 2.5) ────────────────────────────────────────

export interface ProjectTemplate {
  id: string;
  kind: "cli" | "web_api" | "library" | "ui_app" | string;
  title: string;
  description: string;
  goal: string;
  first_iteration: string;
  directions: string;
  advanced: {
    acceptance_criteria: string;
    required_evidence: string;
    forbidden_areas: string;
    risks: any[];
  };
}
