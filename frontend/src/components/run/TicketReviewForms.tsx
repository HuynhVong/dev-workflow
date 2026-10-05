// Approval forms for the ticket review checkpoints: checkout, test plan, a test that needs you, results, Jira comment.
import { useState } from "react";
import { cn } from "@/lib/format";
import { Input, Label, Mono, Pill, Textarea, type Tone } from "../ui";
import { CopyButton } from "./Approval";

type Answer = Record<string, unknown> & { choice?: string };
type Props = { p: Record<string, any>; runId: string; extra: Answer; setExtra: (a: Answer) => void };

const TONE: Record<string, Tone> = { met: "success", passed: "success", partial: "warning", skipped: "muted", unclear: "warning", missing: "destructive", failed: "destructive" };

function Box({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground">
        <span>{title}</span>
        {aside}
      </div>
      {children}
    </div>
  );
}

export function fileUrl(runId: string, path: string) {
  return `/api/runs/${encodeURIComponent(runId)}/file?path=${encodeURIComponent(path)}`;
}

export function Shots({ runId, paths }: { runId: string; paths?: string[] }) {
  if (!paths?.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2" data-testid="proof-shots">
      {paths.map((p) => (
        <a key={p} href={fileUrl(runId, p)} target="_blank" rel="noreferrer" className="block w-40 overflow-hidden rounded-md border border-border bg-surface" title={p.split("/").pop()}>
          <img src={fileUrl(runId, p)} alt={p.split("/").pop()} className="h-24 w-full object-cover object-top" loading="lazy" />
          <div className="truncate px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{p.split("/").pop()}</div>
        </a>
      ))}
    </div>
  );
}

function Notes({ items, tone = "warning" }: { items?: string[]; tone?: "warning" | "destructive" }) {
  if (!items?.length) return null;
  return (
    <ul className={cn("list-disc space-y-0.5 pl-5 text-xs", tone === "warning" ? "text-warning" : "text-destructive")}>
      {items.map((w, i) => (
        <li key={i}>{w}</li>
      ))}
    </ul>
  );
}

export function CheckoutForm({ p, extra, setExtra }: Props) {
  const repos = Object.entries(p.repos ?? {}) as [string, any][];
  return (
    <>
      {p.issues?.length ? (
        <Box title="Not ready yet">
          <Notes tone="destructive" items={p.issues.map((i: any) => `${i.repo}: ${i.problem}`)} />
        </Box>
      ) : null}
      <Box title="In each repo, check out the branch with the commits">
        <div className="space-y-2">
          {repos.map(([name, r]) => {
            const script = (r.commands ?? []).join("\n");
            return (
              <div key={name} className="rounded-lg border border-border bg-surface px-3 py-2" data-testid={`checkout-${name}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Mono className="text-foreground">{name}</Mono>
                  <span className="text-xs text-muted-foreground">now on</span>
                  <Mono>{r.current_branch || "?"}</Mono>
                  <span className="text-xs text-muted-foreground">· commits</span>
                  {(r.commits ?? []).map((c: string) => (
                    <Mono key={c} className="rounded bg-muted px-1">
                      {c.slice(0, 10)}
                    </Mono>
                  ))}
                </div>
                {r.not_in_clone_yet?.length ? <div className="mt-1 text-xs text-warning">Not in your clone yet (fetch first): {r.not_in_clone_yet.join(", ")}</div> : null}
                {r.branches_with_commits?.length ? <div className="mt-1 text-xs text-muted-foreground">On: {r.branches_with_commits.join(", ")}</div> : null}
                <div className="mt-1.5 flex items-start gap-2">
                  <pre className="flex-1 overflow-x-auto rounded bg-muted px-2 py-1.5 font-mono text-[11px]">{script}</pre>
                  <CopyButton text={script} />
                </div>
                {r.run ? (
                  <div className="mt-1 text-xs text-muted-foreground">
                    Start the app: <Mono>{r.run}</Mono>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </Box>
      <div>
        <Label htmlFor="app-url" hint="(where the running app is)">
          App URL
        </Label>
        <Input id="app-url" value={(extra.app_url as string) ?? p.app_url ?? ""} onChange={(e) => setExtra({ ...extra, app_url: e.target.value })} placeholder="http://localhost:5173" data-testid="app-url" />
      </div>
      <Notes items={p.warnings} />
    </>
  );
}

type Case = { id: string; title: string; criterion: string; preconditions: string[]; steps: string[]; expected: string; mode: string; needs_you_reason: string; proof: string[] };

export function TestPlanForm({ p, extra, setExtra }: Props) {
  const [cases, setCases] = useState<Case[]>(p.cases ?? []);
  const [run, setRun] = useState<string[]>((p.cases ?? []).map((c: Case) => c.id));
  const [open, setOpen] = useState<string | null>(null);
  const push = (nextCases: Case[], nextRun: string[], edited: boolean) => {
    setCases(nextCases);
    setRun(nextRun);
    setExtra({ ...extra, run: nextRun, ...(edited || extra.cases ? { cases: nextCases } : {}) });
  };
  const edit = (id: string, patch: Partial<Case>) => push(cases.map((c) => (c.id === id ? { ...c, ...patch } : c)), run, true);
  const add = () => {
    const id = `TC${cases.length + 1}`;
    push([...cases, { id, title: "New case", criterion: "", preconditions: [], steps: [], expected: "", mode: "auto", needs_you_reason: "", proof: [] }], [...run, id], true);
    setOpen(id);
  };
  const crit = (p.coverage?.criteria ?? []) as { criterion: string; status: string; evidence: string }[];
  return (
    <>
      {crit.length ? (
        <Box title="Acceptance criteria in the commits">
          <ul className="space-y-1 text-xs">
            {crit.map((c, i) => (
              <li key={i} className="flex items-start gap-2">
                <Pill tone={TONE[c.status] ?? "muted"}>{c.status}</Pill>
                <span>
                  {c.criterion} <span className="text-muted-foreground">· {c.evidence}</span>
                </span>
              </li>
            ))}
          </ul>
        </Box>
      ) : null}
      {p.reviews ? (
        <Box title="Code review">
          <ul className="space-y-1 text-xs">
            {Object.entries(p.reviews).map(([repo, r]: [string, any]) => (
              <li key={repo}>
                <Mono className="text-foreground">{repo}</Mono> <Pill tone={r.decision === "approve" ? "success" : r.decision === "comment" ? "warning" : "destructive"}>{r.decision.replace("_", " ")}</Pill>{" "}
                <span className="text-muted-foreground">risk {r.risk} · {r.summary}</span>
              </li>
            ))}
          </ul>
        </Box>
      ) : null}
      <Box
        title={`Test cases (${run.length} of ${cases.length} ticked) against ${p.app_url || "the app"}`}
        aside={
          <button type="button" onClick={add} className="text-xs text-primary hover:underline" data-testid="add-case">
            + Add case
          </button>
        }
      >
        <ul className="space-y-1.5" data-testid="test-plan">
          {cases.map((c) => (
            <li key={c.id} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
              <div className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-0.5 accent-[var(--success)]"
                  checked={run.includes(c.id)}
                  onChange={(e) => push(cases, e.target.checked ? [...run, c.id] : run.filter((x) => x !== c.id), false)}
                  aria-label={`Run ${c.id}`}
                  data-testid={`case-${c.id}`}
                />
                <div className="min-w-0 flex-1">
                  <button type="button" className="text-left" onClick={() => setOpen(open === c.id ? null : c.id)}>
                    <Mono className="text-muted-foreground">{c.id}</Mono> <span className="text-foreground">{c.title}</span>
                  </button>
                  {c.mode === "needs_you" ? (
                    <span className="ml-2">
                      <Pill tone="warning">needs you</Pill> <span className="text-muted-foreground">{c.needs_you_reason}</span>
                    </span>
                  ) : null}
                  <div className="text-muted-foreground">{c.criterion}</div>
                  {open === c.id ? (
                    <div className="mt-2 space-y-1.5">
                      <Input value={c.title} onChange={(e) => edit(c.id, { title: e.target.value })} aria-label="Title" />
                      <Input value={c.criterion} onChange={(e) => edit(c.id, { criterion: e.target.value })} aria-label="Criterion" placeholder="Acceptance criterion" />
                      <Textarea rows={Math.max(3, c.steps.length)} value={c.steps.join("\n")} onChange={(e) => edit(c.id, { steps: e.target.value.split("\n") })} aria-label="Steps" placeholder="One step per line" />
                      <Input value={c.expected} onChange={(e) => edit(c.id, { expected: e.target.value })} aria-label="Expected" placeholder="Expected result" />
                      <label className="flex items-center gap-1.5 text-muted-foreground">
                        <input type="checkbox" checked={c.mode === "needs_you"} onChange={(e) => edit(c.id, { mode: e.target.checked ? "needs_you" : "auto" })} /> Needs me at some step
                      </label>
                    </div>
                  ) : (
                    <ol className="mt-1 list-decimal pl-5 text-muted-foreground">
                      {c.steps.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                      {c.expected ? <li className="list-none text-foreground">Expect: {c.expected}</li> : null}
                    </ol>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </Box>
      {p.not_testable?.length ? (
        <Box title="Not testable in the browser">
          <Notes items={p.not_testable} />
        </Box>
      ) : null}
    </>
  );
}

export function HumanStepForm({ p, runId }: Props) {
  return (
    <>
      <div className="rounded-lg border border-warning/50 bg-surface px-3 py-2">
        <div className="text-xs text-muted-foreground">
          <Mono>{p.case}</Mono> {p.title}
        </div>
        <div className="mt-1 text-sm font-medium" data-testid="human-ask">
          {p.ask}
        </div>
        {p.summary && p.summary !== p.ask ? <div className="mt-1 text-xs text-muted-foreground">{p.summary}</div> : null}
        {p.app_url ? (
          <a href={p.app_url} target="_blank" rel="noreferrer" className="mt-1 inline-block text-xs text-primary hover:underline">
            Open {p.app_url}
          </a>
        ) : null}
      </div>
      <Shots runId={runId} paths={p.screenshots} />
    </>
  );
}

export function ResultsForm({ p, runId, extra, setExtra }: Props) {
  const results = (p.results ?? []) as any[];
  const [retest, setRetest] = useState<string[]>(results.filter((r) => r.status !== "passed").map((r) => r.id));
  const toggle = (id: string, on: boolean) => {
    const next = on ? [...retest, id] : retest.filter((x) => x !== id);
    setRetest(next);
    setExtra({ ...extra, retest: next });
  };
  const c = p.counts ?? {};
  return (
    <Box title={`Results: ${c.passed ?? 0} passed, ${c.failed ?? 0} failed, ${c.skipped ?? 0} skipped`} aside={<span>Ticked cases run again on Re-test</span>}>
      <ul className="space-y-2" data-testid="test-results">
        {results.map((r) => (
          <li key={r.id} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <input type="checkbox" className="accent-[var(--warning)]" checked={retest.includes(r.id)} onChange={(e) => toggle(r.id, e.target.checked)} aria-label={`Re-test ${r.id}`} />
              <Pill tone={TONE[r.status] ?? "muted"}>{r.status}</Pill>
              <Mono className="text-muted-foreground">{r.id}</Mono>
              <span className="text-foreground">{r.title}</span>
            </div>
            <div className="mt-0.5 text-muted-foreground">{r.criterion}</div>
            {r.summary ? <div className="mt-1">{r.summary}</div> : null}
            {(r.steps ?? []).filter((s: any) => !s.ok).map((s: any, i: number) => (
              <div key={i} className="mt-0.5 text-destructive">
                ✗ {s.step}
                {s.note ? `: ${s.note}` : ""}
              </div>
            ))}
            <Shots runId={runId} paths={r.screenshots} />
          </li>
        ))}
      </ul>
    </Box>
  );
}

export function CommentForm({ p, runId, extra, setExtra }: Props) {
  const text = (extra.comment as string) ?? p.comment ?? "";
  return (
    <>
      <div className={cn("rounded-lg border px-3 py-2 text-xs", p.will_post ? "border-border bg-surface" : "border-warning/50 bg-warning-soft/40 text-warning")}>
        {p.will_post
          ? `Approve posts this comment${p.jira?.can_attach ? ` and attaches ${p.attachments?.length ?? 0} screenshot(s)` : ""} on the ticket. Nothing has been posted yet.`
          : `Jira posting is off (${p.jira?.reason || "no permission"}). Approve saves the comment and screenshots so you can post them by hand.`}
      </div>
      <Box title="Comment" aside={<CopyButton text={text} label="Copy comment" />}>
        <Textarea
          rows={16}
          className="font-mono text-xs"
          value={text}
          onChange={(e) => setExtra({ ...extra, comment: e.target.value, _edited: e.target.value !== p.comment })}
          data-testid="jira-comment"
        />
      </Box>
      <Box title={`Screenshots (${p.attachments?.length ?? 0})`}>
        <Shots runId={runId} paths={p.attachments} />
      </Box>
    </>
  );
}
