// Turns a workflow's graph plus a run's event history into the execution stepper rows.
// Works for any graph: steps come from the workflow metadata when it has some, else from the graph itself.
import type { DevEvent, RunDetail, Workflow } from "./api";
import { humanize } from "./format";

export type StepState = "done" | "running" | "waiting" | "failed" | "skipped" | "pending";

export interface Exec {
  task: string;
  node: string;
  repo: string;
  startSeq: number;
  start: number;
  end?: number;
  finishSeq?: number;
  error?: string | null;
  result?: unknown;
}

export interface Step {
  id: string;
  title: string;
  detail: string;
  state: StepState;
  execs: Exec[];
  duration: number;
  repos: string[];
  summary: string;
  checkpoint: boolean;
}

const INTERNAL = new Set(["__start__", "__end__"]);

export function baseId(node: string): string {
  return node.endsWith("_wait") ? node.slice(0, -5) : node;
}

export function nodeTitle(wf: Workflow | undefined, node: string): string {
  const id = baseId(node);
  return wf?.nodes?.[id] || wf?.checkpoints?.[id] || humanize(id);
}

/** Executions (start/finish pairs) from the event history, in start order. */
export function executions(events: DevEvent[]): Exec[] {
  const byTask = new Map<string, Exec>();
  const out: Exec[] = [];
  for (const e of events) {
    if (e.kind === "node_started") {
      const ex: Exec = { task: e.data?.task ?? String(e.seq), node: e.node, repo: e.repo, startSeq: e.seq, start: e.at };
      byTask.set(ex.task, ex);
      out.push(ex);
    } else if (e.kind === "node_finished") {
      const ex = byTask.get(e.data?.task);
      if (ex) {
        ex.end = e.at;
        ex.finishSeq = e.seq;
        ex.error = e.data?.error;
        if (e.data && "result" in e.data) ex.result = e.data.result;
        if (!ex.repo && e.repo) ex.repo = e.repo;
      }
    }
  }
  return out;
}

function graphOrder(wf: Workflow): string[] {
  const g = wf.graph;
  if (!g) return [];
  const adj = new Map<string, string[]>();
  for (const e of g.edges) adj.set(e.source, [...(adj.get(e.source) ?? []), e.target]);
  const seen = new Set<string>();
  const order: string[] = [];
  const queue = ["__start__"];
  while (queue.length) {
    const n = queue.shift()!;
    if (seen.has(n)) continue;
    seen.add(n);
    order.push(n);
    for (const t of adj.get(n) ?? []) if (!seen.has(t)) queue.push(t);
  }
  for (const n of g.nodes) if (!seen.has(n.id)) order.push(n.id);
  return order;
}

export function visibleNode(wf: Workflow | undefined, node: string): boolean {
  if (INTERNAL.has(node) || node.startsWith("__")) return false;
  return !(wf?.hidden_nodes ?? []).includes(node);
}

export function buildSteps(wf: Workflow | undefined, run: RunDetail | undefined, events: DevEvent[]): Step[] {
  const execs = executions(events);
  const checkpointNames = new Set(Object.keys(wf?.checkpoints ?? {}));
  const graphNodes = new Set((wf?.graph?.nodes ?? []).map((n) => n.id));
  for (const n of graphNodes) if (n.endsWith("_wait")) checkpointNames.add(baseId(n));

  let ids = (wf?.steps?.length ? wf.steps : wf ? graphOrder(wf) : []).map(baseId);
  ids = ids.filter((id, i) => ids.indexOf(id) === i && visibleNode(wf, id));

  // Nodes that ran but are not in the planned list (a clarify, a scope request, a fix loop) slot in after the step
  // that ran just before them, so the list reads in the order things happened.
  const listed = new Set(ids);
  const lastIndexBySeq = (seq: number) => {
    let at = -1;
    for (const ex of execs) {
      if (ex.startSeq >= seq) break;
      const i = ids.indexOf(baseId(ex.node));
      if (i >= 0) at = Math.max(at, i);
    }
    return at;
  };
  for (const ex of execs) {
    const id = baseId(ex.node);
    if (listed.has(id) || !visibleNode(wf, id) || !visibleNode(wf, ex.node)) continue;
    const at = lastIndexBySeq(ex.startSeq);
    ids.splice(at + 1, 0, id);
    listed.add(id);
  }
  if (run?.pending && !listed.has(baseId(run.pending.name)) && visibleNode(wf, run.pending.name)) ids.push(baseId(run.pending.name));

  const finished = run && ["COMPLETED", "ABORTED"].includes(run.status);
  const waitingName = run?.status === "WAITING_HUMAN" ? baseId(run.pending?.name || run.checkpoint || run.node || "") : "";

  return ids.map((id) => {
    const mine = execs.filter((e) => baseId(e.node) === id);
    const open = mine.filter((e) => e.end === undefined);
    const errors = mine.filter((e) => e.error);
    let state: StepState = "pending";
    if (waitingName === id) state = "waiting";
    else if (run?.status === "RUNNING" && !run.stale && open.length) state = "running";
    else if (run?.status === "FAILED" && (baseId(run.node) === id || (open.length && mine.length))) state = "failed";
    else if (run && (run.status === "RUNNING" && run.stale) && open.length) state = "failed";
    else if (errors.length && errors[errors.length - 1] === mine[mine.length - 1]) state = "failed";
    else if (mine.some((e) => e.end !== undefined)) state = "done";
    else if (finished) state = "skipped";

    const duration = mine.reduce((s, e) => s + ((e.end ?? (state === "running" ? Date.now() / 1000 : e.start)) - e.start), 0);
    const repos = [...new Set(mine.map((e) => e.repo).filter(Boolean))];
    const last = [...mine].reverse().find((e) => e.result !== undefined);
    return {
      id,
      title: nodeTitle(wf, id),
      detail: wf?.node_details?.[id] ?? "",
      state,
      execs: mine,
      duration,
      repos,
      summary: summarize(id, last?.result, run),
      checkpoint: checkpointNames.has(id),
    };
  });
}

/** One short line describing what a node produced, for the common devflow shapes; empty when unknown. */
export function summarize(id: string, result: unknown, run?: RunDetail): string {
  if (!result || typeof result !== "object") return "";
  const r = result as Record<string, any>;
  const n = (x: unknown) => (Array.isArray(x) ? x.length : 0);
  const plural = (k: number, w: string) => `${k} ${w}${k === 1 ? "" : "s"}`;
  if (r.ticket?.title) return r.ticket.title;
  if (r.analysis) return `${plural(n(r.analysis.acceptance_criteria), "criterion")}, ${plural(n(r.analysis.questions), "open question")}`.replace("criterions", "criteria");
  if (r.impact) return `Risk ${r.impact.risk?.level ?? "?"}, ${plural(n(r.impact.candidate_repos), "candidate repo")}`;
  if (r.plan?.repos) {
    const tasks = r.plan.repos.reduce((s: number, x: any) => s + n(x.tasks), 0);
    return `${plural(tasks, "task")} across ${plural(n(r.plan.repos), "repo")}`;
  }
  if (r.decisions && Array.isArray(r.decisions) && r.decisions.length) {
    const d = r.decisions[r.decisions.length - 1];
    return `Answered: ${d.choice}${d.note ? ` (“${String(d.note).slice(0, 60)}”)` : ""}`;
  }
  if (r.scope && id === "prepare_worktrees") return `${plural(Object.keys(r.scope).length, "worktree")} ready`;
  if (r.repos && typeof r.repos === "object") {
    const entries = Object.entries(r.repos as Record<string, any>);
    const st = entries.map(([k, v]) => (v?.status ? `${k} ${String(v.status).replace(/_/g, " ")}` : k));
    if (st.length) return st.join(", ");
  }
  if (r.mrs && typeof r.mrs === "object") return `${plural(Object.keys(r.mrs).length, "draft MR")}`;
  if (r.output) return String(r.output).split("\n")[0].slice(0, 120);
  if (r.pending_feedback && n(r.pending_feedback)) return plural(n(r.pending_feedback), "feedback item");
  const keys = Object.keys(r).filter((k) => !k.startsWith("_") && k !== "run_id");
  void run;
  return keys.length ? `Updated ${keys.slice(0, 4).join(", ")}` : "";
}
