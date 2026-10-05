import { useQuery } from "@tanstack/react-query";
import { Ban, FileText, Pencil, Search, Terminal, Wrench } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, type RunDetail } from "@/lib/api";
import { clock, cn, duration, tokens, usd } from "@/lib/format";
import { summarize, type Exec, type Step } from "@/lib/steps";
import { JsonTree } from "../JsonTree";
import { Empty, Mono, Pill, Select, Spinner, Tabs } from "../ui";
import { ChecksView } from "./Approval";

type Tab = "output" | "diff" | "input" | "logs";

export function NodeDetail({ run, step }: { run: RunDetail; step: Step }) {
  const [pick, setPick] = useState(step.execs.length - 1);
  useEffect(() => setPick(step.execs.length - 1), [step.id, step.execs.length]);
  const exec: Exec | undefined = step.execs[pick];
  const [tab, setTab] = useState<Tab>("output");
  const open = exec && exec.end === undefined;
  const q = useQuery({
    queryKey: ["node", run.run_id, exec?.startSeq],
    queryFn: () => api.node(run.run_id, exec!.startSeq),
    enabled: !!exec,
    refetchInterval: open ? 2500 : false,
  });
  const d = q.data;
  const use = useMemo(() => {
    const t = { input: 0, output: 0, cache_read: 0, cache_write: 0, cost: 0, models: new Set<string>() };
    for (const u of d?.usage ?? []) {
      t.input += u.data.input;
      t.output += u.data.output;
      t.cache_read += u.data.cache_read;
      t.cache_write += u.data.cache_write;
      t.cost += u.data.cost_usd ?? 0;
      if (u.data.model) t.models.add(u.data.model);
    }
    return t;
  }, [d]);

  if (!exec) return null;
  const repos = exec.repo ? [exec.repo] : Object.keys(run.scope ?? {});
  return (
    <div className="mt-4 rounded-xl border border-border bg-card" data-testid="node-detail">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-4 py-3 text-xs text-muted-foreground">
        <span className="text-sm font-semibold text-foreground">{step.title}</span>
        {exec.repo ? <Mono className="rounded bg-muted px-1.5 text-foreground">{exec.repo}</Mono> : null}
        {step.execs.length > 1 ? (
          <Select className="h-7 w-auto py-0 text-xs" value={pick} onChange={(e) => setPick(Number(e.target.value))} aria-label="Execution">
            {step.execs.map((e, i) => (
              <option key={e.startSeq} value={i}>
                Run {i + 1} of {step.execs.length}
                {e.repo ? ` · ${e.repo}` : ""} · {clock(e.start)}
              </option>
            ))}
          </Select>
        ) : null}
        <span>· started {clock(exec.start)}</span>
        {exec.end ? <span>· {duration(exec.end - exec.start)}</span> : <Pill tone="primary" pulse>In progress</Pill>}
        {use.models.size ? <span>· {[...use.models].join(", ")} · medium</span> : null}
        {use.input + use.output ? (
          <span>
            · {tokens(use.input + use.cache_read + use.cache_write)} in / {tokens(use.output)} out{use.cost ? ` · ${usd(use.cost)}` : ""}
          </span>
        ) : null}
        {q.isFetching ? <Spinner className="ml-auto" /> : null}
      </div>
      <div className="px-4 pt-2">
        <Tabs<Tab>
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "output", label: "Output" },
            { id: "diff", label: "Diff" },
            { id: "input", label: "Input" },
            { id: "logs", label: `Logs${d?.activity.length ? ` (${d.activity.length})` : ""}` },
          ]}
        />
      </div>
      <div className="max-h-[520px] overflow-auto p-4">
        {tab === "output" ? (
          exec.error ? (
            <pre className="whitespace-pre-wrap rounded-lg bg-destructive-soft p-3 text-xs text-destructive">{exec.error}</pre>
          ) : d?.finished ? (
            <Output result={d.finished.data?.result} step={step} run={run} />
          ) : (
            <Empty title={open ? "Still working" : "No output recorded"}>{open ? "The output shows when this node finishes. Logs update live." : null}</Empty>
          )
        ) : null}
        {tab === "diff" ? <DiffTab runId={run.run_id} repos={repos} /> : null}
        {tab === "input" ? d?.input ? <JsonTree value={d.input} open={1} /> : <Empty title="Input not available">Older checkpoints may have been pruned.</Empty> : null}
        {tab === "logs" ? <Logs activity={d?.activity ?? []} usage={d?.usage ?? []} /> : null}
      </div>
    </div>
  );
}

function Output({ result, step, run }: { result: unknown; step: Step; run: RunDetail }) {
  if (result === undefined || result === null) return <Empty title="This node returned nothing" />;
  const r = result as Record<string, any>;
  const line = summarize(step.id, result, run);
  return (
    <div className="space-y-4 text-sm">
      {line ? <p className="text-foreground">{line}</p> : null}
      {r.analysis ? (
        <Block title="Requirements">
          <p className="text-xs">{r.analysis.summary}</p>
          <List title="Acceptance criteria" items={r.analysis.acceptance_criteria} />
          <List title="Open questions" items={r.analysis.questions} tone="warning" />
        </Block>
      ) : null}
      {r.context?.requirement ? (
        <Block title="Context">
          <List title="Requirement" items={r.context.requirement.map((x: any) => `${x.text} (${x.source})`)} />
          <List title="How it works today" items={r.context.current_business?.map((x: any) => `${x.text} (${x.source})`)} />
          <List title="Conflicts" items={r.context.conflicts} tone="warning" />
        </Block>
      ) : null}
      {r.plan?.repos ? (
        <Block title="Plan">
          {r.plan.repos.map((x: any) => (
            <div key={x.repo} className="mb-2">
              <Mono className="text-foreground">{x.repo}</Mono>
              <ol className="list-decimal pl-5 text-xs">
                {x.tasks.map((t: string, i: number) => (
                  <li key={i}>{t}</li>
                ))}
              </ol>
            </div>
          ))}
        </Block>
      ) : null}
      {r.dag?.waves ? (
        <Block title="Waves">
          <div className="flex flex-wrap gap-2 text-xs">
            {r.dag.waves.map((w: string[], i: number) => (
              <span key={i} className="rounded border border-border px-2 py-1">
                {i + 1}: <Mono>{w.join(", ")}</Mono>
              </span>
            ))}
          </div>
        </Block>
      ) : null}
      {r.repos && typeof r.repos === "object"
        ? Object.entries(r.repos as Record<string, any>).map(([n, v]) =>
            v?.checks ? (
              <Block key={n} title={`Checks · ${n}`}>
                <ChecksView checks={v.checks} />
                {Object.entries(v.checks as Record<string, any>).map(([c, x]) =>
                  x && typeof x === "object" && (x.tail || x.output) ? (
                    <pre key={c} className="mt-2 max-h-48 overflow-auto rounded bg-surface p-2 font-mono text-[11px] text-muted-foreground">
                      {c}: {String(x.tail ?? x.output)}
                    </pre>
                  ) : null,
                )}
              </Block>
            ) : null,
          )
        : null}
      {r.output ? <pre className="whitespace-pre-wrap rounded-lg bg-surface p-3 text-xs">{String(r.output)}</pre> : null}
      <Block title="Everything this node returned">
        <JsonTree value={result} open={1} />
      </Block>
    </div>
  );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{title}</div>
      {children}
    </div>
  );
}

function List({ title, items, tone }: { title: string; items?: string[]; tone?: "warning" }) {
  if (!items?.length) return null;
  return (
    <div className="mt-2">
      <div className="text-xs font-medium">{title}</div>
      <ul className={cn("mt-0.5 list-disc space-y-0.5 pl-5 text-xs", tone === "warning" && "text-warning")}>
        {items.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

const TOOL_ICON: Record<string, React.ReactNode> = {
  Read: <FileText className="size-3.5" />,
  Edit: <Pencil className="size-3.5" />,
  Write: <Pencil className="size-3.5" />,
  MultiEdit: <Pencil className="size-3.5" />,
  Bash: <Terminal className="size-3.5" />,
  Grep: <Search className="size-3.5" />,
  Glob: <Search className="size-3.5" />,
};

function Logs({ activity, usage }: { activity: { seq: number; at: number; repo: string; data: any }[]; usage: { seq: number; at: number; data: any }[] }) {
  const rows = [...activity.map((a) => ({ ...a, k: "a" as const })), ...usage.map((u) => ({ ...u, repo: "", k: "u" as const }))].sort((a, b) => a.seq - b.seq);
  if (!rows.length) return <Empty title="No activity recorded">Coding nodes log every Claude Code tool call here, live.</Empty>;
  return (
    <ul className="space-y-1 font-mono text-[12px]" data-testid="node-logs">
      {rows.map((r) =>
        r.k === "a" ? (
          <li key={r.seq} className={cn("flex items-start gap-2", r.data.denied && "text-destructive")}>
            <span className="w-16 shrink-0 text-muted-foreground">{clock(r.at)}</span>
            <span className="mt-0.5 shrink-0 text-muted-foreground">{r.data.denied ? <Ban className="size-3.5" /> : TOOL_ICON[r.data.tool] ?? <Wrench className="size-3.5" />}</span>
            <span className="w-16 shrink-0">{r.data.denied ? "Denied" : r.data.tool}</span>
            <span className="min-w-0 break-all">
              {r.data.detail}
              {r.data.reason ? <span className="text-muted-foreground"> ({r.data.reason})</span> : null}
            </span>
          </li>
        ) : (
          <li key={r.seq} className="flex items-start gap-2 text-muted-foreground">
            <span className="w-16 shrink-0">{clock(r.at)}</span>
            <span className="mt-0.5 shrink-0">◆</span>
            <span>
              {r.data.model} · {r.data.step} · {tokens(r.data.input + r.data.cache_read + r.data.cache_write)} in / {tokens(r.data.output)} out
              {r.data.cost_usd != null ? ` · ${usd(r.data.cost_usd)}` : ""}
              {r.data.duration_s ? ` · ${duration(r.data.duration_s)}` : ""}
            </span>
          </li>
        ),
      )}
    </ul>
  );
}

export function DiffTab({ runId, repos }: { runId: string; repos: string[] }) {
  const [repo, setRepo] = useState(repos[0] ?? "");
  useEffect(() => setRepo(repos[0] ?? ""), [repos.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  const q = useQuery({ queryKey: ["diff", runId, repo], queryFn: () => api.diff(runId, repo), enabled: !!repo });
  if (!repos.length) return <Empty title="No repos in this run's scope yet" />;
  return (
    <div className="space-y-3" data-testid="diff-view">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {repos.length > 1 ? (
          <Select className="h-8 w-auto text-xs" value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Repo">
            {repos.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </Select>
        ) : (
          <Mono className="text-foreground">{repo}</Mono>
        )}
        <span>Changes in this run's worktree against origin's base branch, read through rtk git.</span>
        {q.isFetching ? <Spinner /> : null}
      </div>
      {q.data?.note ? <p className="text-xs text-muted-foreground">{q.data.note}</p> : null}
      {q.error ? <p className="text-xs text-destructive">{(q.error as Error).message}</p> : null}
      {q.data && !q.data.diff && !q.data.note ? <Empty title="No changes yet" /> : null}
      {q.data?.diff ? <DiffView text={q.data.diff} /> : null}
    </div>
  );
}

export function DiffView({ text }: { text: string }) {
  const files = useMemo(() => {
    const out: { name: string; lines: string[]; add: number; del: number }[] = [];
    for (const line of text.split("\n")) {
      if (line.startsWith("diff --git ")) {
        const name = line.replace(/^diff --git a\//, "").split(" b/")[0];
        out.push({ name, lines: [], add: 0, del: 0 });
      } else if (out.length) {
        const f = out[out.length - 1];
        if (/^(index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(line)) continue;
        f.lines.push(line);
        if (line.startsWith("+")) f.add++;
        else if (line.startsWith("-")) f.del++;
      }
    }
    return out;
  }, [text]);
  return (
    <div className="space-y-3">
      {files.map((f) => (
        <div key={f.name} className="overflow-hidden rounded-lg border border-border">
          <div className="flex items-center gap-2 border-b border-border bg-surface px-3 py-1.5 font-mono text-[12px]">
            <span className="truncate">{f.name}</span>
            <span className="ml-auto text-success">+{f.add}</span>
            <span className="text-destructive">−{f.del}</span>
          </div>
          <pre className="overflow-x-auto text-[12px] leading-5">
            {f.lines.map((l, i) => (
              <div
                key={i}
                className={cn(
                  "px-3 font-mono",
                  l.startsWith("+") && "bg-success-soft text-success",
                  l.startsWith("-") && "bg-destructive-soft text-destructive",
                  l.startsWith("@@") && "bg-primary-soft text-primary",
                )}
              >
                {l || " "}
              </div>
            ))}
          </pre>
        </div>
      ))}
    </div>
  );
}
