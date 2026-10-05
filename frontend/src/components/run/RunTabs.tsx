import { useQuery } from "@tanstack/react-query";
import { Activity, ExternalLink } from "lucide-react";
import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, type DevEvent, type RunDetail, type Workflow } from "@/lib/api";
import { clock, cn, dateTime, duration, humanize, tokens, usd } from "@/lib/format";
import { executions, nodeTitle, summarize, visibleNode } from "@/lib/steps";
import { JsonTree } from "../JsonTree";
import { Card, Empty, Pill, Segmented } from "../ui";

// ---------------------------------------------------------------------------------------------- Activity
export function ActivityTab({ run, wf, events, onOpenNode }: { run: RunDetail; wf?: Workflow; events: DevEvent[]; onOpenNode: (node: string, seq: number) => void }) {
  const [mode, setMode] = useState<"steps" | "all">("steps");
  const rows = useMemo(() => {
    const out: { key: string; at: number; title: string; sub: string; tone: "primary" | "muted" | "warning" | "destructive" | "success"; node?: string; seq?: number }[] = [];
    const usageBy: Record<string, { t: number; c: number; m: Set<string> }> = {};
    for (const e of events)
      if (e.kind === "usage") {
        const k = `${e.node}|${e.repo}`;
        usageBy[k] ??= { t: 0, c: 0, m: new Set() };
        usageBy[k].t += (e.data.input ?? 0) + (e.data.output ?? 0) + (e.data.cache_read ?? 0) + (e.data.cache_write ?? 0);
        usageBy[k].c += e.data.cost_usd ?? 0;
        if (e.data.model) usageBy[k].m.add(e.data.model);
      }
    for (const ex of executions(events)) {
      if (!visibleNode(wf, ex.node) && !ex.error) continue;
      const u = usageBy[`${ex.node}|${ex.repo}`];
      const parts = [ex.repo, clock(ex.start), ex.end ? duration(ex.end - ex.start) : "running", u?.m.size ? [...u.m].join(", ") : "", u?.t ? `${tokens(u.t)} tok` : ""].filter(Boolean);
      out.push({
        key: `x${ex.startSeq}`,
        at: ex.start,
        title: `${nodeTitle(wf, ex.node)}${ex.error ? " failed" : ""}${ex.end ? "" : " started"}${ex.result !== undefined && summarize(ex.node, ex.result, run) ? `: ${summarize(ex.node, ex.result, run)}` : ""}`,
        sub: ex.error ? String(ex.error).slice(0, 200) : parts.join(" · "),
        tone: ex.error ? "destructive" : ex.end ? "muted" : "primary",
        node: ex.node,
        seq: ex.startSeq,
      });
    }
    for (const e of events) {
      if (e.kind === "decision") out.push({ key: `e${e.seq}`, at: e.at, title: `${nodeTitle(wf, e.data.checkpoint)}: ${humanize(e.data.choice ?? "")}`, sub: e.data.note ? `“${e.data.note}”` : clock(e.at), tone: "success" });
      else if (e.kind === "checkpoint_waiting") out.push({ key: `e${e.seq}`, at: e.at, title: `Waiting for you: ${nodeTitle(wf, e.node)}`, sub: clock(e.at), tone: "warning" });
      else if (e.kind === "status" && ["FAILED", "ABORTED", "COMPLETED"].includes(e.data.status))
        out.push({ key: `e${e.seq}`, at: e.at, title: `Run ${e.data.status.toLowerCase()}`, sub: e.data.detail || clock(e.at), tone: e.data.status === "FAILED" ? "destructive" : e.data.status === "COMPLETED" ? "success" : "muted" });
      else if (mode === "all" && e.kind === "agent_activity")
        out.push({ key: `e${e.seq}`, at: e.at, title: `${e.data.denied ? "Denied " : ""}${e.data.tool}: ${e.data.detail}`, sub: [e.repo, nodeTitle(wf, e.node), clock(e.at)].filter(Boolean).join(" · "), tone: e.data.denied ? "destructive" : "muted" });
      else if (mode === "all" && !["node_started", "node_finished", "usage", "agent_activity", "status", "decision", "checkpoint_waiting"].includes(e.kind))
        out.push({ key: `e${e.seq}`, at: e.at, title: humanize(e.kind), sub: [e.node, e.repo, clock(e.at)].filter(Boolean).join(" · "), tone: "muted" });
    }
    return out.sort((a, b) => b.at - a.at || b.key.localeCompare(a.key));
  }, [events, wf, run, mode]);

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs text-muted-foreground">Newest first. Loops and waves show as separate rows.</span>
        <Segmented
          value={mode}
          onChange={setMode}
          options={[
            { id: "steps", label: "Steps & decisions" },
            { id: "all", label: "Everything" },
          ]}
        />
      </div>
      <ol className="relative" data-testid="activity">
        {rows.map((r, i) => (
          <li key={r.key} className="relative flex gap-3 pb-4">
            {i < rows.length - 1 ? <span className="absolute left-4 top-8 bottom-0 w-px bg-track" aria-hidden /> : null}
            <span
              className={cn(
                "relative z-[1] inline-flex size-8 shrink-0 items-center justify-center rounded-full",
                i === 0 ? "bg-primary-soft text-primary" : r.tone === "destructive" ? "bg-destructive-soft text-destructive" : r.tone === "warning" ? "bg-warning-soft text-warning" : r.tone === "success" ? "bg-success-soft text-success" : "bg-muted text-muted-foreground",
              )}
            >
              <Activity className="size-3.5" />
            </span>
            <button className="min-w-0 pt-1 text-left disabled:cursor-default" disabled={!r.seq} onClick={() => r.seq && r.node && onOpenNode(r.node, r.seq)}>
              <div className="text-sm">{r.title}</div>
              <div className="text-xs text-muted-foreground">{r.sub}</div>
            </button>
          </li>
        ))}
        {!rows.length ? <Empty title="Nothing has happened yet" /> : null}
      </ol>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- Repos
export function ReposTab({ run }: { run: RunDetail }) {
  const names = [...new Set([...Object.keys(run.scope ?? {}), ...Object.keys(run.repos ?? {})])];
  if (!names.length) return <Empty title="No repos in scope yet">They appear once the worktrees are prepared.</Empty>;
  return (
    <div className="overflow-x-auto rounded-lg border border-border" data-testid="repos-tab">
      <table className="w-full text-sm">
        <thead className="bg-surface text-xs text-muted-foreground">
          <tr>
            {["Repo", "Wave", "Status", "Branch", "Fix attempts", "MR", "Worktree"].map((h) => (
              <th key={h} className="px-3 py-2 text-left font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map((n) => {
            const r = run.repos?.[n] ?? {};
            const mr = run.mrs?.[n] as any;
            const url = mr?.url ?? mr?.web_url;
            return (
              <tr key={n} className="border-t border-border">
                <td className="px-3 py-2 font-mono text-[12px]">{n}</td>
                <td className="px-3 py-2 text-xs">{r.wave !== undefined ? r.wave + 1 : ""}</td>
                <td className="px-3 py-2">{r.status ? <Pill tone={r.status === "ready" ? "success" : r.status.startsWith("blocked") ? "destructive" : r.status.includes("fix") ? "warning" : "muted"}>{humanize(r.status)}</Pill> : null}</td>
                <td className="px-3 py-2 font-mono text-[12px]">{r.branch ?? ""}</td>
                <td className="px-3 py-2 text-xs">{r.fix_attempts_used !== undefined ? `${r.fix_attempts_used} of 3` : ""}</td>
                <td className="px-3 py-2 text-xs">
                  {url ? (
                    <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                      Draft MR <ExternalLink className="size-3" />
                    </a>
                  ) : mr ? (
                    "created"
                  ) : (
                    ""
                  )}
                </td>
                <td className="max-w-[260px] truncate px-3 py-2 font-mono text-[11px] text-muted-foreground" title={run.scope?.[n]}>
                  {run.scope?.[n] ?? ""}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- Decisions, side effects, audit
function useAudit(runId: string) {
  return useQuery({ queryKey: ["audit", runId], queryFn: () => api.audit(runId), refetchInterval: 10_000 });
}

export function DecisionsTab({ run, wf }: { run: RunDetail; wf?: Workflow }) {
  const audit = useAudit(run.run_id).data?.audit ?? [];
  const decisions = audit.filter((a) => a.kind === "decision");
  if (!decisions.length) return <Empty title="No decisions yet">Every checkpoint answer shows here with its note and time.</Empty>;
  return (
    <ul className="space-y-2" data-testid="decisions-tab">
      {decisions.map((d, i) => (
        <li key={i} className="rounded-lg border border-border bg-surface px-3 py-2">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{nodeTitle(wf, d.checkpoint)}</span>
            <Pill tone={d.choice === "abort" ? "destructive" : ["approve", "ok"].includes(d.choice) ? "success" : "muted"}>{humanize(d.choice ?? "")}</Pill>
            <span className="ml-auto text-xs text-muted-foreground">{dateTime(d.at)}</span>
          </div>
          {d.note ? <p className="mt-1 whitespace-pre-wrap text-xs text-muted-foreground">{d.note}</p> : null}
          {Object.keys(d).filter((k) => !["at", "kind", "checkpoint", "choice", "note"].includes(k)).length ? (
            <div className="mt-1">
              <JsonTree value={Object.fromEntries(Object.entries(d).filter(([k]) => !["at", "kind", "checkpoint", "choice", "note"].includes(k)))} open={0} />
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function SideEffectsTab({ runId }: { runId: string }) {
  const fx = useAudit(runId).data?.side_effects ?? [];
  if (!fx.length) return <Empty title="No side effects yet">Branches, pushes, MRs and Jira updates are recorded here before they happen.</Empty>;
  return (
    <div className="overflow-x-auto rounded-lg border border-border" data-testid="effects-tab">
      <table className="w-full text-sm">
        <thead className="bg-surface text-xs text-muted-foreground">
          <tr>
            {["Action", "Repo", "Step", "Status", "Result"].map((h) => (
              <th key={h} className="px-3 py-2 text-left font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {fx.map((f) => (
            <tr key={f.id} className="border-t border-border align-top">
              <td className="px-3 py-2">{humanize(f.action)}</td>
              <td className="px-3 py-2 font-mono text-[12px]">{f.repo}</td>
              <td className="px-3 py-2 text-xs text-muted-foreground">{f.step}</td>
              <td className="px-3 py-2">
                <Pill tone={f.status === "done" || f.status === "reconciled" ? "success" : f.status === "intent" ? "warning" : "muted"}>{f.status}</Pill>
              </td>
              <td className="max-w-[320px] px-3 py-2">{f.result ? <JsonTree value={safeJson(f.result)} open={0} /> : null}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function safeJson(s: string) {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

export function AuditTab({ runId }: { runId: string }) {
  const audit = useAudit(runId).data?.audit ?? [];
  return (
    <ul className="space-y-1" data-testid="audit-tab">
      {[...audit].reverse().map((a, i) => {
        const { at, kind, ...rest } = a;
        return (
          <li key={i} className="flex gap-3 border-b border-border/60 py-1.5 text-xs">
            <span className="w-28 shrink-0 text-muted-foreground">{dateTime(at)}</span>
            <span className="w-32 shrink-0 font-medium">{humanize(kind)}</span>
            <div className="min-w-0 flex-1">
              <JsonTree value={rest} open={0} />
            </div>
          </li>
        );
      })}
      {!audit.length ? <Empty title="The audit log is empty" /> : null}
    </ul>
  );
}

// ---------------------------------------------------------------------------------------------- Tokens
export const CHART = { output: "var(--primary)", input: "oklch(78% 0.1 256)", cacheRead: "oklch(52% 0.015 260)", cacheWrite: "oklch(40% 0.015 260)" };

export function TokensTab({ runId, wf }: { runId: string; wf?: Workflow }) {
  const q = useQuery({ queryKey: ["run-usage", runId], queryFn: () => api.runUsage(runId) });
  const [by, setBy] = useState<"node" | "model">("node");
  const d = q.data;
  if (!d) return null;
  if (!d.rows.length) return <Empty title="No Claude calls yet">Each call's tokens and cost appear here as it finishes.</Empty>;
  const t = d.totals;
  const groups = (by === "node" ? d.by_node : d.by_model).map((g) => ({
    name: by === "node" ? nodeTitle(wf, g.key) : g.key,
    input: g.input_tokens,
    output: g.output_tokens,
    cache_read: g.cache_read_tokens,
    cache_write: g.cache_write_tokens,
  }));
  return (
    <div className="space-y-4" data-testid="tokens-tab">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Mini label="Input" value={tokens(t.input_tokens)} />
        <Mini label="Output" value={tokens(t.output_tokens)} />
        <Mini label="Cache hit rate" value={`${Math.round(t.cache_hit_rate * 100)}%`} sub={`${tokens(t.cache_read_tokens)} read · ${tokens(t.cache_write_tokens)} written`} />
        <Mini label="Estimated cost" value={usd(t.cost_usd) || "$0.00"} sub={t.unpriced_calls ? `${t.unpriced_calls} calls have no price set` : `${t.calls} calls`} />
      </div>
      <Card className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-sm font-medium">Tokens by {by}</span>
          <Segmented value={by} onChange={setBy} options={[{ id: "node", label: "Node" }, { id: "model", label: "Model" }]} />
        </div>
        <TokenBars data={groups} />
      </Card>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <thead className="bg-surface text-muted-foreground">
            <tr>
              {["Time", "Node", "Repo", "Model", "In", "Out", "Cache read", "Cost", "Duration"].map((h) => (
                <th key={h} className="px-3 py-2 text-left font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.rows.map((u, i) => (
              <tr key={i} className="border-t border-border">
                <td className="px-3 py-1.5 text-muted-foreground">{clock(u.at)}</td>
                <td className="px-3 py-1.5">{nodeTitle(wf, u.node)}</td>
                <td className="px-3 py-1.5 font-mono">{u.repo}</td>
                <td className="px-3 py-1.5 font-mono">{u.model}</td>
                <td className="px-3 py-1.5 font-mono">{tokens(u.input_tokens)}</td>
                <td className="px-3 py-1.5 font-mono">{tokens(u.output_tokens)}</td>
                <td className="px-3 py-1.5 font-mono">{tokens(u.cache_read_tokens)}</td>
                <td className="px-3 py-1.5 font-mono">{u.cost_usd == null ? "—" : usd(u.cost_usd)}</td>
                <td className="px-3 py-1.5 font-mono">{duration(u.duration_s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function Mini({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card className="p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-bold tracking-tight">{value}</div>
      {sub ? <div className="mt-0.5 text-[11px] text-muted-foreground">{sub}</div> : null}
    </Card>
  );
}

export function TokenBars({ data, height = 260 }: { data: { name: string; input: number; output: number; cache_read: number; cache_write: number }[]; height?: number }) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ left: 0, right: 8, top: 4, bottom: 4 }}>
        <CartesianGrid stroke="var(--border)" vertical={false} />
        <XAxis dataKey="name" tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} interval={0} angle={data.length > 6 ? -25 : 0} textAnchor={data.length > 6 ? "end" : "middle"} height={data.length > 6 ? 60 : 30} stroke="var(--border)" />
        <YAxis tickFormatter={(v) => tokens(v)} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} stroke="var(--border)" width={48} />
        <Tooltip
          cursor={{ fill: "var(--muted)", opacity: 0.4 }}
          contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
          formatter={(v) => tokens(Number(v))}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Bar dataKey="input" name="Input" stackId="t" fill={CHART.input} />
        <Bar dataKey="output" name="Output" stackId="t" fill={CHART.output} />
        <Bar dataKey="cache_read" name="Cache read" stackId="t" fill={CHART.cacheRead} />
        <Bar dataKey="cache_write" name="Cache write" stackId="t" fill={CHART.cacheWrite} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

