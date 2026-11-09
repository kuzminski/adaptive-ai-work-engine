/**
 * AAW Agentic Studio — Typed API Client
 *
 * Transports requests to AAW loopback server (product_server.py)
 * Enforces per-launch X-AAW-Token authentication and provides
 * deterministic typed responses.
 */

import {
  ApiError,
  BootstrapData,
  DecisionAction,
  EvidenceView,
  MCPConfigureRequest,
  MCPConfigureResponse,
  MCPStatus,
  RepoInspectResult,
  RoadmapDiff,
  RunDetail,
  RunsResponse,
  StartTaskPayload,
  TaskStartResult,
} from "./types";

export class ApiClient {
  private explicitToken: string | null = null;
  private baseUrl: string = "";

  constructor(options?: { baseUrl?: string; token?: string }) {
    if (options?.baseUrl) {
      this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    }
    if (options?.token) {
      this.explicitToken = options.token;
    }
  }

  /**
   * Set or override the AAW loopback launch token.
   */
  public setToken(token: string | null): void {
    this.explicitToken = token;
  }

  /**
   * Retrieve the current AAW launch token.
   * Priority:
   * 1. Explicitly configured token
   * 2. <meta name="aaw-token" content="..."> in document head
   */
  public getToken(): string {
    if (this.explicitToken) {
      return this.explicitToken;
    }
    if (typeof document !== "undefined") {
      const meta = document.querySelector('meta[name="aaw-token"]');
      const content = meta?.getAttribute("content");
      if (content && content !== "__AAW_TOKEN__") {
        return content;
      }
    }
    return "";
  }

  /**
   * Internal generic fetch request handler.
   */
  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const token = this.getToken();
    if (!token) {
      throw new ApiError(
        "AAW authentication token is missing. Ensure the application is served by the AAW product server.",
        403
      );
    }

    const headers: Record<string, string> = {
      Accept: "application/json",
      "X-AAW-Token": token,
      ...((options.headers as Record<string, string>) || {}),
    };

    if (options.body && typeof options.body === "string" && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }

    const url = `${this.baseUrl}${endpoint.startsWith("/") ? endpoint : `/${endpoint}`}`;

    let response: Response;
    try {
      response = await fetch(url, {
        ...options,
        headers,
      });
    } catch (err: any) {
      throw new ApiError(
        `Network error connecting to AAW server at ${url}: ${err?.message || String(err)}`,
        0,
        err
      );
    }

    let bodyData: any = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try {
        bodyData = await response.json();
      } catch {
        bodyData = null;
      }
    } else {
      try {
        bodyData = await response.text();
      } catch {
        bodyData = null;
      }
    }

    if (!response.ok) {
      const errorMsg =
        (bodyData && typeof bodyData === "object" && bodyData.error) ||
        (typeof bodyData === "string" && bodyData) ||
        `HTTP ${response.status} ${response.statusText}`;

      if (response.status === 403) {
        throw new ApiError(
          `AAW Token authentication failed (HTTP 403 Forbidden): ${errorMsg}. Check loopback security posture.`,
          403,
          bodyData
        );
      }

      throw new ApiError(errorMsg, response.status, bodyData);
    }

    return bodyData as T;
  }

  // ── Public API Methods ───────────────────────────────────────────────────

  /**
   * GET /api/bootstrap
   * Returns application metadata, limits, provider health, presets, and settings.
   */
  public async getBootstrap(): Promise<BootstrapData> {
    return this.request<BootstrapData>("/api/bootstrap", { method: "GET" });
  }

  /**
   * GET /api/runs (with fallback to /api/home)
   * Returns all active, paused, attention, and completed runs grouped by section.
   */
  public async getRuns(): Promise<RunsResponse> {
    try {
      return await this.request<RunsResponse>("/api/runs", { method: "GET" });
    } catch (err: any) {
      if (err instanceof ApiError && err.status === 404) {
        return this.request<RunsResponse>("/api/home", { method: "GET" });
      }
      throw err;
    }
  }

  /**
   * GET /api/runs/<id>
   * Returns real-time state, process graph, timeline, and human gate status for a run.
   */
  public async getRun(id: string): Promise<RunDetail> {
    const encoded = encodeURIComponent(id);
    return this.request<RunDetail>(`/api/runs/${encoded}`, { method: "GET" });
  }

  /**
   * GET /api/runs/<id>/evidence
   * Returns unified diff, journal events, ledger proof, and execution artifacts.
   */
  public async getEvidence(
    id: string,
    executionId?: string
  ): Promise<EvidenceView> {
    const encoded = encodeURIComponent(id);
    const query = executionId ? `?execution_id=${encodeURIComponent(executionId)}` : "";
    return this.request<EvidenceView>(`/api/runs/${encoded}/evidence${query}`, {
      method: "GET",
    });
  }

  /**
   * POST /api/runs/<id>/decision
   * Submits a Human Gate decision (ACCEPT, REJECT, or CONTINUE) with optional feedback.
   */
  public async postDecision(
    id: string,
    action: DecisionAction,
    feedback?: string,
    options?: { mode?: string }
  ): Promise<any> {
    const encoded = encodeURIComponent(id);
    const body: Record<string, any> = {
      action,
      feedback: feedback || "",
      reason: feedback || "",
      mode: action === "CONTINUE" ? (options?.mode || "direction") : undefined,
    };

    try {
      return await this.request<any>(`/api/runs/${encoded}/decision`, {
        method: "POST",
        body: JSON.stringify(body),
      });
    } catch (err: any) {
      // If server does not have the combined /decision route, fall back to /accept, /reject, /continue
      if (err instanceof ApiError && err.status === 404) {
        const subAction = action.toLowerCase();
        return this.request<any>(`/api/runs/${encoded}/${subAction}`, {
          method: "POST",
          body: JSON.stringify(body),
        });
      }
      throw err;
    }
  }

  /**
   * POST /api/runs/<id>/stop
   * Requests safe graceful or forced stop of a running task.
   */
  public async postStop(id: string, force: boolean = false): Promise<any> {
    const encoded = encodeURIComponent(id);
    return this.request<any>(`/api/runs/${encoded}/stop`, {
      method: "POST",
      body: JSON.stringify({ force: !!force }),
    });
  }

  /**
   * POST /api/runs/<id>/resume
   * Resumes an interrupted or paused run.
   */
  public async postResume(
    id: string,
    lockToken?: string | null
  ): Promise<any> {
    const encoded = encodeURIComponent(id);
    return this.request<any>(`/api/runs/${encoded}/resume`, {
      method: "POST",
      body: JSON.stringify({ lock_token: lockToken || null }),
    });
  }

  /**
   * GET /api/mcp/status
   * Checks configuration status of Antigravity, Claude, and Codex MCP endpoints.
   */
  public async getMCPStatus(): Promise<MCPStatus> {
    return this.request<MCPStatus>("/api/mcp/status", { method: "GET" });
  }

  /**
   * POST /api/mcp/configure
   * Generates or installs MCP config files for target AI clients.
   */
  public async configureMCP(
    targets?: string | MCPConfigureRequest
  ): Promise<MCPConfigureResponse> {
    const payload =
      typeof targets === "string"
        ? { target: targets, install: true }
        : { target: targets?.target || "all", install: targets?.install ?? true };

    return this.request<MCPConfigureResponse>("/api/mcp/configure", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  /**
   * POST /api/start
   * Launches a new autonomous AAW run.
   */
  public async postStart(
    payload: StartTaskPayload | { form: any }
  ): Promise<TaskStartResult> {
    const body = "form" in payload ? payload : { form: payload };
    try {
      return await this.request<TaskStartResult>("/api/start", {
        method: "POST",
        body: JSON.stringify(body),
      });
    } catch (err: any) {
      if (err instanceof ApiError && err.status === 404) {
        return this.request<TaskStartResult>("/api/tasks/start", {
          method: "POST",
          body: JSON.stringify(body),
        });
      }
      throw err;
    }
  }

  // ── Supporting Utility Endpoints ─────────────────────────────────────────

  /**
   * GET /api/ping
   */
  public async ping(): Promise<{ ok: boolean; version: string }> {
    return this.request<{ ok: boolean; version: string }>("/api/ping", {
      method: "GET",
    });
  }

  /**
   * POST /api/repo/inspect
   */
  public async inspectRepo(path: string): Promise<RepoInspectResult> {
    return this.request<RepoInspectResult>("/api/repo/inspect", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
  }

  /**
   * POST /api/pick-folder
   */
  public async pickFolder(): Promise<{ path: string | null; error?: string }> {
    return this.request<{ path: string | null; error?: string }>(
      "/api/pick-folder",
      {
        method: "POST",
        body: JSON.stringify({}),
      }
    );
  }

  /**
   * POST /api/providers/detect
   */
  public async detectProviders(): Promise<any> {
    return this.request<any>("/api/providers/detect", {
      method: "POST",
      body: JSON.stringify({}),
    });
  }

  /**
   * GET /api/settings
   */
  public async getSettings(): Promise<Record<string, any>> {
    return this.request<Record<string, any>>("/api/settings", {
      method: "GET",
    });
  }

  /**
   * POST /api/settings
   */
  public async saveSettings(
    settings: Record<string, any>
  ): Promise<Record<string, any>> {
    return this.request<Record<string, any>>("/api/settings", {
      method: "POST",
      body: JSON.stringify(settings),
    });
  }

  /**
   * GET /api/experience — benchmark built passively from this machine's real runs
   * (per task kind: cost x solve rate, Pareto front, recommendations, provenance).
   */
  public async getExperience(params: { kind?: string; persona?: string } = {}): Promise<any> {
    const query = new URLSearchParams();
    if (params.kind) query.set("kind", params.kind);
    // the backend's demo switch is `demo=<persona>` (a clearly labelled synthetic preview)
    if (params.persona) query.set("demo", params.persona);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return this.request<any>(`/api/experience${suffix}`, { method: "GET" });
  }

  /**
   * POST /api/experience/forecast — before START: what history says about work like this goal
   * (horizon, cost range, recommended implementation model with an `apply` hint).
   */
  public async postForecast(body: Record<string, any>): Promise<any> {
    return this.request<any>("/api/experience/forecast", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /**
   * POST /api/intake/propose — one confirmed read-only planner call that turns a loose idea
   * into scope + roadmap + risks, with a forecast. Costs one planner call; the UI must confirm first.
   */
  public async postIntake(body: { idea: string; repo?: string; chain_mode?: boolean; planning?: string }): Promise<any> {
    return this.request<any>("/api/intake/propose", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /**
   * POST /api/roadmap/diff — explicit diff between the previous run's roadmap points and the edited ones (roadmap 2.2).
   */
  public async roadmapDiff(previous: string[], current: string[]): Promise<RoadmapDiff> {
    return this.request<RoadmapDiff>("/api/roadmap/diff", {
      method: "POST",
      body: JSON.stringify({ previous, current }),
    });
  }

  /**
   * POST /api/models/verify — probe which configured models actually answer on this machine.
   */
  public async verifyModels(body: Record<string, any> = {}): Promise<any> {
    return this.request<any>("/api/models/verify", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }
}

// Singleton client instance for application-wide use
export const client = new ApiClient();
