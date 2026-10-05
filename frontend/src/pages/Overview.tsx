import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ChevronRight, Coins, Filter, Info, Plus, Search, ShieldCheck, Zap } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { RunDetailCard } from "@/components/run/RunDetail";
import { Button, Card, Chip, Empty, IconSquare, Input, Mono, Select, StatusPill, type Tone } from "@/components/ui";
import { api, type Run, type Workflow } from "@/lib/api";
import { ago, cn, duration, pad2, runTitle, tokens, usd } from "@/lib/format";
import { useMeta, useNow, useRuns, useWorkflowMap } from "@/lib/hooks";

type Filter = "all" | "active" | "needs" | "failed" | "completed";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "needs", label: "Needs you" },
  { id: "failed", label: "Failed" },
  { id: "completed", label: "Completed" },
];

function startOfDay() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

function matches(r: Run, f: Filter) {
  if (f === "active") return ["RUNNING", "PENDING", "WAITING_HUMAN"].includes(r.status);
  if (f === "needs") return r.status === "WAITING_HUMAN";
  if (f === "failed") return r.status === "FAILED" || r.stale;
  if (f === "completed") return r.status === "COMPLETED" || r.status === "ABORTED";
  return true;
}

const RANK: Record<string, number> = { WAITING_HUMAN: 0, RUNNING: 1, PENDING: 2, FAILED: 3, COMPLETED: 4, ABORTED: 5 };

export function Overview({ onNewRun }: { onNewRun: (workflow?: string) => void }) {
  const [params, setParams] = useSearchParams();
  const filter = (params.get("filter") as Filter) || "all";
  const workflow = params.get("workflow") || "";
  const selected = params.get("run") || "";
  const runsQ = useRuns();
  const runs = runsQ.data?.runs ?? [];
  const meta = useMeta().data;
  const wfs = useWorkflowMap();
  const day = useMemo(startOfDay, []);
  const usage = useQuery({ queryKey: ["usage", "today", day], queryFn: () => api.usage(day), refetchInterval: 30_000 });
  const [q, setQ] = useState("");

  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v);
    else p.delete(k);
    setParams(p, { replace: k === "run" ? false : true });
  };

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return runs
      .filter((r) => matches(r, filter) && (!workflow || r.workflow === workflow))
      .filter((r) => !needle || [r.ticket, r.label, r.run_id, r.workflow].some((x) => (x || "").toLowerCase().includes(needle)))
      .sort((a, b) => (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9) || b.updated_at - a.updated_at);
  }, [runs, filter, workflow, q]);

  // Keep a run selected on wide screens, so the detail card is never empty when there is something to show.
  useEffect(() => {
    if (!selected && shown.length && window.matchMedia("(min-width: 1280px)").matches) set("run", shown[0].run_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, shown.length]);

  const count = (f: Filter) => runs.filter((r) => matches(r, f)).length;
  const running = runs.filter((r) => r.status === "RUNNING" && !r.stale).length;
  const needs = count("needs");
  const doneToday = runs.filter((r) => r.status === "COMPLETED" && r.updated_at >= day).length;
  const failed = runs.filter((r) => r.status === "FAILED" || r.stale).length;
  const tt = usage.data?.totals;
  const slots = meta?.slots;

  return (
    <div className="mx-auto max-w-[1400px]">
      <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-[11px] font-medium uppercase tracking-widest">
            <span className="size-1.5 animate-slow-pulse rounded-full bg-primary" />
            <span className="text-primary">Live workspace</span>
            {slots ? (
              <span className="normal-case tracking-normal text-muted-foreground">
                {slots.running} of {slots.max} slots busy{slots.queued ? ` · ${slots.queued} queued` : ""}
              </span>
            ) : null}
          </div>
          <h1 className="text-[28px] font-bold tracking-tight sm:text-[32px]">
            {filter === "needs" ? "Approvals" : workflow ? wfs[workflow]?.title ?? workflow : filter !== "all" || params.has("filter") ? "All runs" : "Workflow overview"}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">Monitor your runs and keep work moving.</p>
        </div>
        <Button variant="primary" onClick={() => onNewRun(workflow || undefined)} data-testid="new-run">
          <Plus className="size-4" /> New run
        </Button>
      </div>

      <div className={cn("mb-6 grid grid-cols-2 gap-3 sm:gap-4", failed ? "lg:grid-cols-5" : "lg:grid-cols-4")}>
        <Stat label="Running" tone="primary" icon={<Zap className="size-4" />} value={pad2(running)} caption="Working right now" onClick={() => set("filter", "active")} />
        <Stat label="Needs you" tone="warning" icon={<ShieldCheck className="size-4" />} value={pad2(needs)} caption="Waiting on your decision" onClick={() => set("filter", "needs")} />
        <Stat label="Completed" tone="success" icon={<CheckCircle2 className="size-4" />} value={pad2(doneToday)} caption="Finished today" onClick={() => set("filter", "completed")} />
        <Stat
          label="Tokens today"
          tone="muted"
          icon={<Coins className="size-4" />}
          value={tokens((tt?.input_tokens ?? 0) + (tt?.output_tokens ?? 0) + (tt?.cache_read_tokens ?? 0) + (tt?.cache_write_tokens ?? 0))}
          caption={tt ? `${usd(tt.cost_usd)} estimated${tt.unpriced_calls ? ", some unpriced" : ""}` : "No calls yet"}
        />
        {failed ? (
          <Stat label="Failed" tone="destructive" icon={<AlertTriangle className="size-4" />} value={pad2(failed)} caption="Resumable" onClick={() => set("filter", "failed")} />
        ) : null}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(340px,2fr)_3fr]">
        <Card className={cn("flex min-w-0 flex-col", selected && "hidden xl:flex")}>
          <div className="flex items-start justify-between gap-3 p-5 pb-3">
            <div>
              <h2 className="text-base font-semibold">Runs</h2>
              <p className="text-xs text-muted-foreground">Waiting on you first, then running.</p>
            </div>
            <span className="text-sm text-muted-foreground">{shown.length}</span>
          </div>
          <div className="space-y-3 px-5 pb-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input className="pl-9" placeholder="Search runs…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search runs" />
            </div>
            <div className="flex flex-wrap items-center gap-1">
              <Filter className="mr-1 size-3.5 text-muted-foreground" />
              {FILTERS.map((f) => (
                <Chip key={f.id} active={filter === f.id} onClick={() => set("filter", f.id === "all" ? "" : f.id)}>
                  {f.label}
                </Chip>
              ))}
            </div>
            <Select value={workflow} onChange={(e) => set("workflow", e.target.value)} aria-label="Workflow" className="h-8 text-xs">
              <option value="">Every workflow</option>
              {Object.values(wfs).map((w) => (
                <option key={w.id} value={w.id}>
                  {w.title}
                </option>
              ))}
            </Select>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto border-t border-border" data-testid="runs-list">
            {shown.map((r) => (
              <RunRow key={r.run_id} run={r} wf={wfs[r.workflow]} selected={r.run_id === selected} onClick={() => set("run", r.run_id)} />
            ))}
            {!shown.length && !runsQ.isLoading ? (
              <Empty title={runs.length ? "No runs match" : "No runs yet"}>
                {runs.length ? "Try another filter." : "Start one with New run. Runs started from the CLI show here too."}
              </Empty>
            ) : null}
          </div>
        </Card>
        <div className={cn("min-w-0", !selected && "hidden xl:block")}>
          {selected ? (
            <RunDetailCard runId={selected} compact onBack={() => set("run", "")} />
          ) : (
            <Card className="h-full">
              <Empty title="Select a run">Its steps, decisions and tokens show here.</Empty>
            </Card>
          )}
        </div>
      </div>
      <p className="mt-6 flex items-center gap-1.5 text-xs text-muted-foreground">
        <Info className="size-3.5" /> Runs started from the CLI show here too.
      </p>
    </div>
  );
}

function Stat({ label, tone, icon, value, caption, onClick }: { label: string; tone: Tone; icon: ReactNode; value: string; caption: string; onClick?: () => void }) {
  return (
    <Card className={cn("p-4 sm:p-5", onClick && "cursor-pointer transition-colors duration-150 hover:border-accent")} onClick={onClick} data-testid={`stat-${label.toLowerCase().replace(/ /g, "-")}`}>
      <div className="flex items-start justify-between">
        <span className="text-sm text-muted-foreground">{label}</span>
        <IconSquare tone={tone}>{icon}</IconSquare>
      </div>
      <div className="mt-2 text-[28px] font-bold leading-none tracking-tight">{value}</div>
      <div className="mt-2 truncate text-xs text-muted-foreground">{caption}</div>
    </Card>
  );
}

function RunRow({ run: r, wf, selected, onClick }: { run: Run; wf?: Workflow; selected: boolean; onClick: () => void }) {
  const now = useNow();
  const title = runTitle(r);
  const cp = r.pending ? wf?.checkpoints?.[r.pending.name] ?? r.pending.name : "";
  const node = wf?.nodes?.[r.node] ?? r.node;
  const tok = (r.tokens?.input ?? 0) + (r.tokens?.output ?? 0) + (r.tokens?.cache_read ?? 0) + (r.tokens?.cache_write ?? 0);
  return (
    <button
      onClick={onClick}
      data-testid={`run-row-${r.run_id}`}
      className={cn(
        "flex w-full items-center gap-3 border-b border-l-2 border-b-border px-5 py-3 text-left transition-colors duration-150",
        selected ? "border-l-primary bg-primary-soft" : "border-l-transparent hover:bg-muted/40",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-semibold">{r.ticket ? `${r.ticket} · ${title}` : title}</span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="size-1.5 shrink-0 rounded-full" style={{ background: wf?.color ?? "var(--muted-foreground)" }} />
          <span className="truncate">
            {wf?.title ?? r.workflow} · <Mono>{r.run_id}</Mono>
          </span>
        </div>
        <div className="mt-1 truncate text-xs">
          {r.status === "WAITING_HUMAN" ? (
            <span className="text-warning">
              {cp} · waiting {ago(r.waiting_since ?? r.updated_at, now).replace(" ago", "")}
            </span>
          ) : r.status === "RUNNING" && !r.stale ? (
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <span className="size-1.5 animate-slow-pulse rounded-full bg-primary" />
              {node}
              {Object.entries(r.repos ?? {}).slice(0, 4).map(([n, s]) => (
                <span key={n} className={cn("rounded px-1 font-mono text-[11px]", s.status === "ready" || s.implemented ? "bg-success-soft text-success" : "bg-muted")}>
                  {n}
                </span>
              ))}
              <span>· {duration(now - r.created_at)}</span>
              {tok ? <span>· {tokens(tok)} tok</span> : null}
            </span>
          ) : r.status === "FAILED" || r.stale ? (
            <span className="text-destructive">
              {node ? `${node}: ` : ""}
              {r.stale ? "the process driving it stopped" : r.detail}
            </span>
          ) : r.status === "PENDING" ? (
            <span className="text-muted-foreground">{r.queued ? "Queued for a free slot" : "Not started"}</span>
          ) : r.status === "ABORTED" && r.detail ? (
            <span className="text-muted-foreground">Aborted: {r.detail}</span>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1.5">
        <StatusPill status={r.status} stale={r.stale} queued={r.queued} />
        <span className="text-[11px] text-muted-foreground">{ago(r.updated_at, now)}</span>
      </div>
      <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
    </button>
  );
}

