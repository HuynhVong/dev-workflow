import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Clock, GitBranch, Maximize2, MoreHorizontal, Play, RotateCcw, Workflow as GraphIcon, ListOrdered, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type DevEvent, type RunDetail, type Workflow } from "@/lib/api";
import { ago, cn, humanize, runTitle, tokens, usd } from "@/lib/format";
import { useLocal, useMeta, useNow, useRun, useRunEvents, useWorkflowMap } from "@/lib/hooks";
import { baseId, buildSteps, executions, type Step } from "@/lib/steps";
import { Avatar } from "../Shell";
import { Button, Card, Empty, ErrorNote, Mono, Pill, Segmented, Spinner, StatusPill, Tabs } from "../ui";
import { AbortDialog, ApprovalPanel, CopyButton } from "./Approval";
import { GraphView } from "./GraphView";
import { NodeDetail } from "./NodeDetail";
import { ActivityTab, AuditTab, DecisionsTab, ReposTab, SideEffectsTab, TokensTab } from "./RunTabs";
import { Stepper } from "./Stepper";

type Tab = "overview" | "activity" | "repos" | "decisions" | "effects" | "audit" | "tokens";
const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "activity", label: "Activity" },
  { id: "repos", label: "Repos" },
  { id: "decisions", label: "Decisions" },
  { id: "effects", label: "Side effects" },
  { id: "audit", label: "Audit log" },
  { id: "tokens", label: "Tokens" },
];

export function RunDetailCard({ runId, compact, onBack }: { runId: string; compact?: boolean; onBack?: () => void }) {
  const q = useRun(runId);
  const ev = useRunEvents(runId);
  const wfs = useWorkflowMap();
  const run = q.data;
  const wf = run ? wfs[run.workflow] : undefined;
  const [tab, setTab] = useState<Tab>("overview");
  const [more, setMore] = useState(false);
  const [sel, setSel] = useState<string | undefined>();
  useEffect(() => {
    setTab("overview");
    setSel(undefined);
  }, [runId]);

  if (q.error) return <Card className="p-6"><ErrorNote error={q.error} /></Card>;
  if (!run) return <Card className="flex h-64 items-center justify-center"><Spinner className="text-muted-foreground" /></Card>;

  const shownTabs = compact ? TABS.slice(0, 2) : TABS;
  const hidden = compact ? TABS.slice(2) : [];
  return (
    <Card className="min-w-0" data-testid="run-detail">
      <Header run={run} wf={wf} compact={compact} onBack={onBack} />
      <div className="relative flex items-end px-5 pt-2 sm:px-6">
        <Tabs<Tab> className="flex-1" tabs={hidden.some((t) => t.id === tab) ? [...shownTabs, TABS.find((t) => t.id === tab)!] : shownTabs} value={tab} onChange={setTab} />
        {hidden.length ? (
          <div className="relative -mb-px border-b border-border pb-1.5">
            <button className="flex items-center gap-1 rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-muted hover:text-foreground" onClick={() => setMore(!more)} data-testid="more-tabs">
              More <MoreHorizontal className="size-4" />
            </button>
            {more ? (
              <>
                <div className="fixed inset-0 z-30" onClick={() => setMore(false)} />
                <div className="absolute right-0 z-40 mt-1 w-44 rounded-lg border border-border bg-popover p-1 shadow-2xl">
                  {hidden.map((t) => (
                    <button
                      key={t.id}
                      className={cn("block w-full rounded-md px-2.5 py-1.5 text-left text-sm hover:bg-muted", tab === t.id && "text-primary")}
                      onClick={() => {
                        setTab(t.id);
                        setMore(false);
                      }}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className="p-5 sm:p-6">
        {tab === "overview" ? <OverviewTab run={run} wf={wf} events={ev.data ?? []} sel={sel} setSel={setSel} /> : null}
        {tab === "activity" ? <ActivityTab run={run} wf={wf} events={ev.data ?? []} onOpenNode={(node) => {
              setSel(baseId(node));
              setTab("overview");
            }} /> : null}
        {tab === "repos" ? <ReposTab run={run} /> : null}
        {tab === "decisions" ? <DecisionsTab run={run} wf={wf} /> : null}
        {tab === "effects" ? <SideEffectsTab runId={run.run_id} /> : null}
        {tab === "audit" ? <AuditTab runId={run.run_id} /> : null}
        {tab === "tokens" ? <TokensTab runId={run.run_id} wf={wf} /> : null}
      </div>
    </Card>
  );
}

function cliCommand(run: RunDetail): string {
  if (run.status === "WAITING_HUMAN" && run.pending) {
    return run.pending.generic ? `devflow answer ${run.run_id} --value '...'` : `devflow answer ${run.run_id} --choice ${run.pending.options[0]}`;
  }
  if (run.status === "FAILED" || run.stale) return `devflow resume ${run.run_id}`;
  if (run.status === "ABORTED") return `devflow resume --reopen ${run.run_id}`;
  return `devflow status ${run.run_id}`;
}

function Header({ run, wf, compact, onBack }: { run: RunDetail; wf?: Workflow; compact?: boolean; onBack?: () => void }) {
  const now = useNow();
  const meta = useMeta().data;
  const tok = (run.tokens?.input ?? 0) + (run.tokens?.output ?? 0) + (run.tokens?.cache_read ?? 0) + (run.tokens?.cache_write ?? 0);
  const repoCount = Object.keys(run.scope ?? run.repos ?? {}).length;
  const desc = (run.ticket_description || "").split("\n").find((l) => l.trim()) || wf?.description || "";
  return (
    <div className="border-b border-border p-5 pb-4 sm:p-6 sm:pb-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            {onBack ? (
              <button className="rounded-md p-0.5 text-muted-foreground hover:text-foreground xl:hidden" onClick={onBack} aria-label="Back to runs">
                <ArrowLeft className="size-4" />
              </button>
            ) : null}
            <Mono className="truncate text-muted-foreground">
              {run.ticket ? `${run.ticket} · ` : ""}
              {wf?.title ?? run.workflow} · {run.run_id}
            </Mono>
          </div>
          <h2 className="mt-1 text-xl font-bold tracking-tight" data-testid="run-title">
            {runTitle(run)}
          </h2>
          {desc ? <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{desc}</p> : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {run.status === "WAITING_HUMAN" ? <Pill tone="warning">Needs approval</Pill> : <StatusPill status={run.status} stale={run.stale} queued={run.queued} />}
          {compact ? (
            <Link to={`/runs/${encodeURIComponent(run.run_id)}`} className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground" title="Open full width" aria-label="Open full width">
              <Maximize2 className="size-4" />
            </Link>
          ) : null}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <Clock className="size-3.5" /> Started {ago(run.created_at, now)}
        </span>
        <span className="flex items-center gap-1.5">
          <Avatar name={meta?.user || "devflow"} className="size-5 text-[9px]" /> {meta?.user || "You"}
        </span>
        {repoCount ? (
          <span className="flex items-center gap-1.5">
            <GitBranch className="size-3.5" /> {repoCount} repo{repoCount === 1 ? "" : "s"}
          </span>
        ) : null}
        <span data-testid="run-tokens">
          {tokens(tok)} tok{run.tokens?.cost_usd ? ` · ${usd(run.tokens.cost_usd)}` : ""}
          {run.tokens && run.tokens.priced === false ? " (some unpriced)" : ""}
        </span>
        <CopyButton text={cliCommand(run)} label="Copy CLI command" />
      </div>
    </div>
  );
}

function OverviewTab({ run, wf, events, sel, setSel }: { run: RunDetail; wf?: Workflow; events: DevEvent[]; sel?: string; setSel: (s: string | undefined) => void }) {
  const steps = useMemo(() => buildSteps(wf, run, events), [wf, run, events]);
  const execs = useMemo(() => executions(events), [events]);
  const [view, setView] = useLocal<"steps" | "graph">(`devflow.view.${run.workflow}`, "steps");
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["run", run.run_id] });
    qc.invalidateQueries({ queryKey: ["runs"] });
  };
  const abort = useMutation({ mutationFn: (n: string) => api.abort(run.run_id, n), onSuccess: () => (setConfirm(false), refresh()) });
  const resume = useMutation({ mutationFn: (reopen: boolean) => api.resume(run.run_id, reopen), onSuccess: refresh });

  const current = steps.findIndex((s) => s.state === "running" || s.state === "waiting" || s.state === "failed");
  const cur = current >= 0 ? steps[current] : undefined;
  const doneCount = steps.filter((s) => s.state === "done").length;
  const stepNo = cur ? current + 1 : Math.min(doneCount, steps.length);
  const live = cur?.execs.filter((e) => e.end === undefined) ?? [];
  const where = cur ? [cur.title, live.map((e) => e.repo).filter(Boolean).join(", "), live[0]?.repo && run.repos?.[live[0].repo]?.wave !== undefined ? `wave ${run.repos[live[0].repo].wave! + 1}` : ""].filter(Boolean).join(" · ") : "";
  const stateLine =
    run.status === "WAITING_HUMAN" ? "Awaiting input" : run.status === "RUNNING" ? (run.stale ? "Stalled" : "Running") : run.status === "PENDING" ? (run.queued ? "Queued" : "Not started") : humanize(run.status.toLowerCase());
  const selected: Step | undefined = steps.find((s) => s.id === sel);
  const canAbort = ["RUNNING", "WAITING_HUMAN", "PENDING", "FAILED"].includes(run.status) && !(run.status === "RUNNING" && !run.active && !run.stale);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold">Execution</h3>
          <p className="truncate text-xs text-muted-foreground" data-testid="step-line">
            {cur || !["COMPLETED", "ABORTED"].includes(run.status) ? `Step ${stepNo} of ${steps.length}` : `${doneCount} of ${steps.length} steps ran`} · {stateLine}
            {where ? ` · ${where}` : ""}
          </p>
        </div>
        <Segmented
          value={view}
          onChange={setView}
          options={[
            { id: "steps", label: <><ListOrdered className="size-3.5" /> Steps</> },
            { id: "graph", label: <><GraphIcon className="size-3.5" /> Graph</> },
          ]}
        />
        {run.status === "FAILED" || run.stale ? (
          <Button size="sm" variant="primary" disabled={resume.isPending} onClick={() => resume.mutate(false)} data-testid="resume">
            {resume.isPending ? <Spinner /> : <Play className="size-3.5" />} Resume
          </Button>
        ) : null}
        {run.status === "ABORTED" ? (
          <Button size="sm" variant="outline" disabled={resume.isPending} onClick={() => resume.mutate(true)} data-testid="reopen">
            <RotateCcw className="size-3.5" /> Reopen
          </Button>
        ) : null}
        {canAbort ? (
          <Button size="sm" variant="danger-outline" onClick={() => setConfirm(true)} data-testid="abort">
            <X className="size-3.5" /> Abort
          </Button>
        ) : null}
      </div>
      <ErrorNote error={resume.error} />
      {run.status === "RUNNING" && !run.active && !run.stale ? <p className="mb-2 text-xs text-muted-foreground">This run is being driven by another process (the CLI). It shows here live; control it there.</p> : null}
      {run.status === "PENDING" && run.queued ? <p className="mb-2 text-xs text-muted-foreground">Queued: it starts when one of the parallel slots frees up.</p> : null}

      {view === "graph" && wf?.graph ? (
        <>
          <GraphView wf={wf} steps={steps} execs={execs} selected={sel} onSelect={(id) => setSel(id === sel ? undefined : id)} />
          {run.pending ? <ApprovalPanel run={run} wf={wf} /> : null}
        </>
      ) : (
        <Stepper
          steps={steps}
          run={run}
          selected={sel}
          onSelect={(s) => setSel(s.id === sel ? undefined : s.id)}
          after={(s) => (run.pending && s.state === "waiting" ? <ApprovalPanel run={run} wf={wf} /> : null)}
        />
      )}
      {run.pending && view === "steps" && !steps.some((s) => s.state === "waiting") ? <ApprovalPanel run={run} wf={wf} /> : null}

      {selected && selected.execs.length ? <NodeDetail run={run} step={selected} /> : null}
      {selected && !selected.execs.length && view === "graph" ? <p className="mt-3 text-xs text-muted-foreground">{selected.title} has not run in this run.</p> : null}

      {run.status === "COMPLETED" && run.output ? (
        <div className="mt-4 rounded-xl border border-success/40 bg-success-soft/40 p-4">
          <div className="mb-1 text-sm font-semibold text-success">Summary</div>
          <pre className="whitespace-pre-wrap font-sans text-sm" data-testid="run-output">{run.output}</pre>
        </div>
      ) : null}
      {run.status === "ABORTED" ? <p className="mt-3 text-xs text-muted-foreground">Aborted{run.checkpoint ? ` at ${run.checkpoint}` : ""}{run.detail ? `: ${run.detail}` : ""}. Branches, worktrees and edits were left untouched.</p> : null}
      {run.warnings?.length && run.pending?.name !== "approve_plan" ? (
        <div className="mt-4 text-xs text-warning">
          {run.warnings.map((w, i) => (
            <p key={i}>{w}</p>
          ))}
        </div>
      ) : null}
      {!steps.length && !run.pending ? <Empty title="Nothing to show yet" /> : null}
      <AbortDialog open={confirm} onClose={() => setConfirm(false)} onConfirm={(n) => abort.mutate(n)} pending={abort.isPending} error={abort.error} runId={run.run_id} />
    </div>
  );
}
