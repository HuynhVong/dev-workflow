// Typed access to the local devflow server (src/dev_workflows/ui/server.py).

export type Status = "PENDING" | "RUNNING" | "WAITING_HUMAN" | "COMPLETED" | "FAILED" | "ABORTED";

export interface Slots { max: number; running: number; queued: number }

export interface DoctorSummary { ok: number; warn: number; fail: number; blocking: number; status?: string }

export interface Meta {
  workspace_path: string;
  workspace_exists: boolean;
  loaded: boolean;
  load_error: string;
  slots: Slots;
  worktree_root: string;
  state_dir: string;
  repos: string[];
  doctor: DoctorSummary | null;
  user: string;
}

export interface Check {
  id: string;
  group: string;
  label: string;
  status: "ok" | "warn" | "fail";
  blocking: boolean;
  detail: string;
  fix: string;
  data: Record<string, unknown>;
}

export interface DoctorResult { checks: Check[]; summary: DoctorSummary; at: number }

export interface FormField {
  name: string;
  label: string;
  type: "ticket" | "repos" | "text" | "textarea" | "select" | "bool" | "number" | "list" | "json" | "date" | string;
  required?: boolean;
  placeholder?: string;
  help?: string;
  options?: string[];
  default?: unknown;
  rows?: number;
  mono?: boolean;
}

export interface Workflow {
  id: string;
  title: string;
  description: string;
  kind: "ticket" | "jira" | "graph";
  source: string;
  icon: string;
  color: string;
  nodes: Record<string, string>;
  node_details: Record<string, string>;
  steps: string[];
  hidden_nodes: string[];
  form: FormField[] | null;
  generated_form?: boolean;
  checkpoints: Record<string, string>;
  graph?: { nodes: { id: string }[]; edges: { source: string; target: string; conditional: boolean }[] };
  input_schema?: unknown;
  error?: string;
}

export interface Tokens { input: number; output: number; cache_write: number; cache_read: number; cost_usd: number; priced: boolean }

export interface Pending {
  name: string;
  payload: unknown;
  options: string[];
  generic?: boolean;
  error?: string;
}

export interface RepoLive { status?: string; fix_attempts_used?: number; branch?: string; implemented?: boolean; pushed_sha?: string; wave?: number }

export interface Run {
  run_id: string;
  workflow: string;
  ticket: string;
  status: Status;
  node: string;
  checkpoint: string;
  detail: string;
  created_at: number;
  updated_at: number;
  heartbeat_at: number;
  label: string;
  stale: boolean;
  queued: boolean;
  active: boolean;
  tokens: Partial<Tokens>;
  pending?: Pending | null;
  repos?: Record<string, RepoLive>;
  next?: string[];
  waiting_since?: number | null;
  ticket_info?: { key: string; title: string; url: string };
  state_error?: string;
}

export interface RunDetail extends Run {
  inputs: Record<string, unknown> | null;
  scope?: Record<string, string>;
  dag?: { nodes: string[]; edges: [string, string][]; waves: string[][]; merge_order: string[] } | null;
  decisions?: ({ checkpoint: string; choice?: string; note?: string } & Record<string, unknown>)[];
  mrs?: Record<string, { url?: string; iid?: number | string; web_url?: string } & Record<string, unknown>>;
  warnings?: string[];
  output?: string;
  repo_state?: Record<string, Record<string, unknown>>;
  scope_gaps?: unknown[];
  ticket_description?: string;
}

export interface DevEvent {
  seq: number;
  run_id: string;
  at: number;
  kind: string;
  node: string;
  repo: string;
  data: any;
}

export interface UsageRow {
  run_id: string;
  at: number;
  node: string;
  repo: string;
  step: string;
  model: string;
  source: string;
  input_tokens: number;
  output_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  cost_usd: number | null;
  duration_s: number | null;
}

export interface UsageSum {
  input_tokens: number;
  output_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
  calls: number;
  unpriced_calls: number;
  duration_s: number;
  cache_hit_rate: number;
}

export type UsageGroup = UsageSum & { key: string } & Record<string, unknown>;

export interface RunUsage { rows: UsageRow[]; totals: UsageSum; by_node: UsageGroup[]; by_model: UsageGroup[] }

export interface UsageAgg {
  totals: UsageSum;
  by_day: UsageGroup[];
  by_workflow: UsageGroup[];
  by_model: UsageGroup[];
  by_step: UsageGroup[];
  priced_models: string[];
}

export interface NodeDetail {
  node: string;
  repo: string;
  started: DevEvent;
  finished: DevEvent | null;
  activity: DevEvent[];
  usage: DevEvent[];
  input: unknown;
}

export interface Worktree {
  ticket: string;
  repo: string;
  path: string;
  branch: string;
  dirty: boolean | null;
  size_bytes?: number | null;
  run: Run | null;
  can_clean: boolean;
}

export interface Skills {
  dirs: string[];
  installed: { name: string; description: string }[];
  steps: { step: string; model: string; tier: string; skills: string[]; stack: boolean; domain: boolean; escalate_to: string | null; missing: string[] }[];
  models: Record<string, string>;
  effort: string;
}

export interface WorkspaceDoc { path: string; exists: boolean; data: Record<string, any>; env_refs: Record<string, string>; mask: string; yaml: string }

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!res.ok) {
    let code = "error";
    let message = res.statusText;
    try {
      const j = await res.json();
      const d = j.detail ?? j;
      code = d.error ?? code;
      message = typeof d === "string" ? d : d.detail ?? d.error ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  meta: () => call<Meta>("GET", "/api/meta"),
  doctor: () => call<DoctorResult>("GET", "/api/doctor"),
  runDoctor: (body: { repos?: string[]; ping_models?: boolean; workflow?: string } = {}) => call<DoctorResult>("POST", "/api/doctor/run", body),
  workspace: () => call<WorkspaceDoc>("GET", "/api/workspace"),
  saveWorkspace: (data: Record<string, unknown>) => call<WorkspaceDoc>("PUT", "/api/workspace", { data }),
  skills: () => call<Skills>("GET", "/api/skills"),
  workflows: () => call<{ workflows: Workflow[]; errors: string[] }>("GET", "/api/workflows"),
  refreshWorkflows: () => call<{ workflows: Workflow[]; errors: string[] }>("POST", "/api/workflows/refresh"),
  runs: () => call<{ runs: Run[]; slots: Slots }>("GET", "/api/runs"),
  run: (id: string) => call<RunDetail>("GET", `/api/runs/${encodeURIComponent(id)}`),
  uploadImage: (name: string, data: string) => call<{ id: string; name: string; size: number }>("POST", "/api/uploads", { name, data }),
  startRun: (workflow: string, values: Record<string, unknown>) =>
    call<{ run_id: string; queued: boolean }>("POST", "/api/runs", { workflow, values }),
  answer: (id: string, body: Record<string, unknown>) => call<{ ok: boolean }>("POST", `/api/runs/${encodeURIComponent(id)}/answer`, body),
  resume: (id: string, reopen = false) => call<{ ok: boolean }>("POST", `/api/runs/${encodeURIComponent(id)}/resume`, { reopen }),
  feedbackImage: (id: string, name: string, data: string) => call<{ path: string }>("POST", `/api/runs/${encodeURIComponent(id)}/feedback-images`, { name, data }),
  abort: (id: string, note = "") => call<{ status: string }>("POST", `/api/runs/${encodeURIComponent(id)}/abort`, { note }),
  events: (id: string, after = 0) => call<{ events: DevEvent[] }>("GET", `/api/runs/${encodeURIComponent(id)}/events?after=${after}`),
  audit: (id: string) =>
    call<{ audit: ({ at: number; kind: string } & Record<string, any>)[]; side_effects: { id: string; step: string; repo: string; action: string; status: string; result: string | null }[] }>(
      "GET",
      `/api/runs/${encodeURIComponent(id)}/audit`,
    ),
  node: (id: string, seq: number) => call<NodeDetail>("GET", `/api/runs/${encodeURIComponent(id)}/nodes/${seq}`),
  diff: (id: string, repo: string) =>
    call<{ repo: string; diff: string; files: string[]; path?: string; note?: string }>("GET", `/api/runs/${encodeURIComponent(id)}/diff?repo=${encodeURIComponent(repo)}`),
  runUsage: (id: string) => call<RunUsage>("GET", `/api/runs/${encodeURIComponent(id)}/usage`),
  usage: (since = 0) => call<UsageAgg>("GET", `/api/usage?since=${since}`),
  worktrees: () => call<{ worktrees: Worktree[]; root: string }>("GET", "/api/worktrees"),
  cleanWorktree: (ticket: string, repo: string) => call<{ removed: string }>("POST", "/api/worktrees/clean", { ticket, repo }),
};
