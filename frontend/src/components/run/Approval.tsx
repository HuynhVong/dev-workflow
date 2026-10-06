// The "Human in the loop" panel: one form per devflow checkpoint, a generic one for any other interrupt.
// Buttons come only from the options the checkpoint offers; the server and the graph re-validate every answer.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, ExternalLink, ShieldCheck, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { api, type Pending, type RunDetail, type Workflow } from "@/lib/api";
import { cn, humanize } from "@/lib/format";
import { JsonTree } from "../JsonTree";
import { Button, ErrorNote, IconSquare, Label, Mono, Modal, Pill, Spinner, Textarea } from "../ui";
import { ConfirmTicketsForm, ReportForm } from "./StandupForms";
import { CheckoutForm, CommentForm, HumanStepForm, ResultsForm, Shots, TestPlanForm } from "./TicketReviewForms";

type Answer = Record<string, unknown> & { choice?: string };

const APPROVING = new Set(["approve", "ok", "reuse", "fixed_by_hand", "retry", "ready", "continue", "accept"]);

export function ApprovalPanel({ run, wf }: { run: RunDetail; wf?: Workflow }) {
  const pc = run.pending!;
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const [extra, setExtra] = useState<Answer>({});
  const [confirmAbort, setConfirmAbort] = useState(false);
  const send = useMutation({
    mutationFn: (a: Answer) => api.answer(run.run_id, a),
    onSuccess: () => {
      setNote("");
      setExtra({});
      qc.invalidateQueries({ queryKey: ["run", run.run_id] });
      qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const abort = useMutation({
    mutationFn: (n: string) => api.abort(run.run_id, n),
    onSuccess: () => {
      setConfirmAbort(false);
      qc.invalidateQueries({ queryKey: ["run", run.run_id] });
      qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
  const title = wf?.checkpoints?.[pc.name] ?? humanize(pc.name);
  const p = (pc.payload ?? {}) as Record<string, any>;
  const answer = (choice: string) => {
    const { _edited, note: formNote, ...rest } = extra;
    void _edited;
    const n = [formNote as string | undefined, note.trim()].filter(Boolean).join("\n\n");
    send.mutate({ choice, ...rest, ...(n ? { note: n } : {}) });
  };
  const busy = send.isPending || run.active;

  const form = FORMS[pc.name];
  return (
    <div className="mt-2 rounded-xl border border-warning/50 bg-warning-soft/40 p-4 sm:p-5" data-testid="approval-panel" data-checkpoint={pc.name}>
      <div className="flex items-start gap-3">
        <IconSquare tone="warning">
          <ShieldCheck className="size-4" />
        </IconSquare>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-[15px] font-semibold">{pc.generic ? "Input required" : "Approval required"}</h3>
            <span className="text-[11px] font-medium uppercase tracking-widest text-warning">Human in the loop</span>
          </div>
          <p className="mt-0.5 text-sm text-muted-foreground">{title}</p>
        </div>
      </div>
      {pc.error ? <div className="mt-3"><ErrorNote error={pc.error} /></div> : null}
      <div className="mt-4 space-y-4 text-sm">
        {pc.generic ? (
          <Generic pc={pc} onAnswer={(v) => send.mutate(v)} busy={busy} />
        ) : form ? (
          form({ p, run, extra, setExtra })
        ) : (
          <Section title="Details">
            <JsonTree value={pc.payload} open={2} />
          </Section>
        )}
        {p.hint ? <p className="text-xs text-muted-foreground">{p.hint}</p> : null}
        {!pc.generic ? (
          <div>
            <Label htmlFor="decision-note" hint={NOTE_HINT[pc.name] ?? "(optional)"}>
              Decision note
            </Label>
            <Textarea id="decision-note" value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder="Recorded in the audit log with your answer" />
          </div>
        ) : null}
        <ErrorNote error={send.error} />
        {!pc.generic ? (
          <div className="flex flex-wrap gap-2" data-testid="checkpoint-actions">
            {pc.options
              .filter((o) => o !== "abort")
              .sort((a, b) => Number(APPROVING.has(b)) - Number(APPROVING.has(a)))
              .map((o) => {
                const blocked = BLOCKED[pc.name]?.(o, p, note, extra);
                return (
                  <Button
                    key={o}
                    variant={APPROVING.has(o) || (pc.name === "clarify" && o === "answer") ? "success" : "outline"}
                    disabled={busy || !!blocked}
                    title={blocked || undefined}
                    onClick={() => answer(o)}
                    data-testid={`choice-${o}`}
                  >
                    {send.isPending && send.variables?.choice === o ? <Spinner /> : APPROVING.has(o) ? <Check className="size-4" /> : null}
                    {pc.name === "approve_comment" && o === "approve" && !p.will_post ? "Save for posting by hand" : (LABELS[pc.name]?.[o] ?? LABELS._[o] ?? humanize(o))}
                  </Button>
                );
              })}
            {pc.options.includes("abort") || run.status === "WAITING_HUMAN" ? (
              <Button variant="danger-outline" disabled={busy} onClick={() => setConfirmAbort(true)} data-testid="choice-abort">
                <X className="size-4" /> Abort
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
      <AbortDialog open={confirmAbort} onClose={() => setConfirmAbort(false)} onConfirm={(n) => abort.mutate(n)} pending={abort.isPending} error={abort.error} runId={run.run_id} />
    </div>
  );
}

export function AbortDialog({ open, onClose, onConfirm, pending, error, runId }: { open: boolean; onClose: () => void; onConfirm: (note: string) => void; pending: boolean; error: unknown; runId: string }) {
  const [n, setN] = useState("");
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Abort ${runId}?`}
      subtitle="The run stops before any later side effect. Local branches, worktrees and edits are left untouched, and an aborted run can be reopened."
      footer={
        <>
          <Button variant="outline" onClick={onClose}>
            Keep running
          </Button>
          <Button variant="danger" disabled={pending} onClick={() => onConfirm(n)} data-testid="confirm-abort">
            {pending ? <Spinner /> : <X className="size-4" />} Abort run
          </Button>
        </>
      }
    >
      <Label htmlFor="abort-note" hint="(optional)">
        Note
      </Label>
      <Textarea id="abort-note" value={n} onChange={(e) => setN(e.target.value)} rows={2} placeholder="Why, for the audit log" />
      <div className="mt-2">
        <ErrorNote error={error} />
      </div>
    </Modal>
  );
}

const LABELS: Record<string, Record<string, string>> = {
  _: { approve: "Approve & continue", ok: "It works", feedback: "Send feedback", revise: "Revise", proceed: "Proceed without answers", deny: "Deny", retry: "Retry", reuse: "Reuse branches", fixed_by_hand: "Fixed by hand, re-run checks", answer: "Answer", fix: "Fix", skip: "Skip", edit: "Apply my triage" },
  clarify: { answer: "Send answers" },
  approve_push: { approve: "Approve push" },
  approve_code: { approve: "Approve code, go test it", changes: "Request changes" },
  route_ask: { answer: "Answer", fix: "Fix in repos", skip: "Skip these items" },
  triage: { approve: "Approve proposed actions" },
  checkout_gate: { ready: "Checked out, verify" },
  approve_test_plan: { approve: "Run ticked cases", regenerate: "Rewrite the plan" },
  human_step: { continue: "Done, continue", skip: "Skip this case", fail: "Mark failed" },
  review_results: { accept: "Accept results", retest: "Re-test ticked" },
  approve_comment: { approve: "Approve & post to Jira", edit: "Use my edits", regenerate: "Rewrite" },
  confirm_tickets: { continue: "Write the report" },
  review_report: { accept: "Save report", edit: "Use my edits", regenerate: "Rewrite" },
};

const NOTE_HINT: Record<string, string> = {
  approve_plan: "(required to revise)",
  manual_test: "(required for feedback)",
  approve_code: "(required to request changes)",
  manual_retest: "(required for feedback)",
  route_ask: "(the answer, or the fix instructions)",
  triage: "(extra instructions for every fix)",
  approve_test_plan: "(test users or data the tester needs; required to rewrite)",
  human_step: "(the value the test needs, e.g. the OTP; or why it failed)",
  review_results: "(passed on to the re-test)",
  approve_comment: "(required to rewrite)",
  review_report: "(what to change; required to rewrite)",
};

const BLOCKED: Record<string, (o: string, p: Record<string, any>, note: string, extra: Answer) => string | null> = {
  approve_plan: (o, p, note) => (o === "approve" && p.validation_errors?.length ? "The plan has validation errors; revise it" : o === "revise" && !note.trim() ? "Say what to change in the note" : null),
  manual_test: (o, _p, note) => (o === "feedback" && !note.trim() ? "Describe what is wrong in the note" : null),
  approve_code: (o, _p, note) => (o === "changes" && !note.trim() ? "Say what to change in the note" : null),
  manual_retest: (o, _p, note) => (o === "feedback" && !note.trim() ? "Describe what is wrong in the note" : null),
  route_ask: (o, _p, note, extra) => (o === "answer" && !note.trim() ? "Write the answer in the note" : o === "fix" && !(extra.repos as string[] | undefined)?.length ? "Pick the repos to fix" : null),
  triage: (o, _p, _note, extra) => (o === "approve" && extra._edited ? "You changed some actions: use Apply my triage" : o === "edit" && !extra._edited ? "Change an action in the table first" : null),
  clarify: (o, _p, note, extra) => (o === "answer" && !note.trim() && !(extra.note as string) ? "Answer at least one question" : null),
  checkout_gate: (o, p, _note, extra) => (o === "ready" && !String(extra.app_url ?? p.app_url ?? "").trim() ? "Give the app URL" : null),
  approve_test_plan: (o, _p, note) => (o === "regenerate" && !note.trim() ? "Say what to change in the note" : null),
  review_results: (o, _p, _note, extra) => (o === "retest" && Array.isArray(extra.retest) && !extra.retest.length ? "Tick the cases to re-test" : null),
  approve_comment: (o, _p, note, extra) =>
    o === "approve" && extra._edited ? "You edited the comment: use “Use my edits” first" : o === "edit" && !extra._edited ? "Edit the comment first" : o === "regenerate" && !note.trim() ? "Say what to change in the note" : null,
  review_report: (o, _p, note, extra) =>
    o === "accept" && extra._edited ? "You edited the report: use “Use my edits” first" : o === "edit" && !extra._edited ? "Edit the report first" : o === "regenerate" && !note.trim() ? "Say what to change in the note" : null,
};

type FormProps = { p: Record<string, any>; run: RunDetail; extra: Answer; setExtra: (a: Answer) => void };

const FORMS: Record<string, (f: FormProps) => ReactNode> = {
  clarify: ({ p, extra, setExtra }) => <ClarifyForm p={p} extra={extra} setExtra={setExtra} />,
  approve_plan: ({ p, run }) => <PlanView p={p} run={run} />,
  approve_code: ({ p }) => <CodeView p={p} />,
  branch_ownership: ({ p }) => (
    <Section title="Branches that already exist">
      <IssueList items={p.issues} />
    </Section>
  ),
  scope_request: ({ p }) => (
    <Section title="Repos outside this run's scope">
      <ul className="space-y-2">
        {(p.requests ?? []).map((r: any, i: number) => (
          <li key={i} className="rounded-lg border border-border bg-surface px-3 py-2">
            <Mono className="text-foreground">{r.repo}</Mono>
            {r.from_repo ? <span className="text-xs text-muted-foreground"> · asked by {r.from_repo}</span> : null}
            <div className="text-xs text-muted-foreground">{r.reason}</div>
          </li>
        ))}
      </ul>
    </Section>
  ),
  budget_exhausted: ({ p }) => (
    <Section title="Fix attempts used up">
      {Object.entries(p.repos ?? {}).map(([repo, r]: [string, any]) => (
        <div key={repo} className="rounded-lg border border-border bg-surface px-3 py-2">
          <div className="flex items-center gap-2">
            <Mono className="text-foreground">{repo}</Mono>
            <Pill tone="destructive">{r.fix_attempts_used ?? "?"} of 3 attempts</Pill>
          </div>
          {r.summary ? <p className="mt-1 text-xs text-muted-foreground">{r.summary}</p> : null}
          {r.checks ? <ChecksView checks={r.checks} /> : null}
        </div>
      ))}
    </Section>
  ),
  manual_test: (f) => <ManualTest {...f} />,
  manual_retest: (f) => <ManualTest {...f} />,
  route_ask: ({ p, run, extra, setExtra }) => (
    <>
      <Section title="Feedback the router could not place">
        <ul className="space-y-2">
          {(p.items ?? []).map((it: any, i: number) => (
            <li key={i} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
              <div className="text-foreground">{it.cause ?? it.text ?? JSON.stringify(it)}</div>
              <div className="mt-0.5 text-muted-foreground">
                {it.source}
                {it.category ? ` · ${humanize(it.category)}` : ""}
                {it.repos?.length ? ` · ${it.repos.join(", ")}` : ""}
              </div>
            </li>
          ))}
        </ul>
      </Section>
      <RepoPicker label="Repos to fix (for Fix)" repos={Object.keys(run.scope ?? run.repos ?? {})} value={(extra.repos as string[]) ?? []} onChange={(r) => setExtra({ ...extra, repos: r })} />
    </>
  ),
  approve_push: ({ p }) => <PushView p={p} />,
  push_blocked: ({ p }) => (
    <Section title="Push problems">
      <IssueList items={p.issues} />
    </Section>
  ),
  sync_blocked: ({ p }) => (
    <Section title="Branch sync problems">
      <IssueList items={p.issues} />
    </Section>
  ),
  triage: ({ p, extra, setExtra }) => <TriageForm p={p} extra={extra} setExtra={setExtra} />,
  checkout_gate: ({ p, run, extra, setExtra }) => <CheckoutForm p={p} runId={run.run_id} extra={extra} setExtra={setExtra} />,
  approve_test_plan: ({ p, run, extra, setExtra }) => <TestPlanForm key={JSON.stringify(p.cases?.map((c: any) => c.title))} p={p} runId={run.run_id} extra={extra} setExtra={setExtra} />,
  human_step: ({ p, run, extra, setExtra }) => <HumanStepForm p={p} runId={run.run_id} extra={extra} setExtra={setExtra} />,
  review_results: ({ p, run, extra, setExtra }) => <ResultsForm key={JSON.stringify(p.results?.map((r: any) => r.id + r.status))} p={p} runId={run.run_id} extra={extra} setExtra={setExtra} />,
  approve_comment: ({ p, run, extra, setExtra }) => <CommentForm p={p} runId={run.run_id} extra={extra} setExtra={setExtra} />,
  confirm_tickets: ({ p, extra, setExtra }) => <ConfirmTicketsForm key={JSON.stringify(p.tickets?.map((t: any) => t.key))} p={p} extra={extra} setExtra={setExtra} />,
  review_report: ({ p, extra, setExtra }) => <ReportForm p={p} extra={extra} setExtra={setExtra} />,
};

function Section({ title, children, aside }: { title: ReactNode; children: ReactNode; aside?: ReactNode }) {
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

function IssueList({ items }: { items: unknown }) {
  const list = Array.isArray(items) ? items : items ? [items] : [];
  return (
    <ul className="space-y-1.5">
      {list.map((it, i) => (
        <li key={i} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
          {typeof it === "string" ? it : <JsonTree value={it} open={1} />}
        </li>
      ))}
      {!list.length ? <li className="text-xs text-muted-foreground">None listed.</li> : null}
    </ul>
  );
}

function ClarifyForm({ p, extra, setExtra }: { p: Record<string, any>; extra: Answer; setExtra: (a: Answer) => void }) {
  const qs: string[] = p.questions ?? [];
  const [answers, setAnswers] = useState<string[]>(qs.map(() => ""));
  const update = (i: number, v: string) => {
    const next = [...answers];
    next[i] = v;
    setAnswers(next);
    const text = qs.map((q, j) => (next[j].trim() ? `Q: ${q}\nA: ${next[j].trim()}` : "")).filter(Boolean).join("\n\n");
    setExtra({ ...extra, note: text || undefined });
  };
  return (
    <>
      <Section title={`Open questions (${qs.length})`}>
        <div className="space-y-3">
          {qs.map((q, i) => (
            <div key={i}>
              <div className="mb-1 text-sm">{q}</div>
              <Textarea rows={2} value={answers[i]} onChange={(e) => update(i, e.target.value)} placeholder="Your answer" data-testid={`clarify-answer-${i}`} />
            </div>
          ))}
        </div>
      </Section>
      {p.conflicts?.length ? (
        <Section title="Confluence and Jira disagree">
          <ul className="list-disc space-y-1 pl-5 text-xs text-warning">
            {p.conflicts.map((c: string, i: number) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </Section>
      ) : null}
      {p.scope_requests?.length ? (
        <RepoPicker
          label="Approve these repos outside scope"
          repos={p.scope_requests.map((r: any) => r.repo)}
          hints={Object.fromEntries(p.scope_requests.map((r: any) => [r.repo, r.reason]))}
          value={(extra.approve_repos as string[]) ?? []}
          onChange={(r) => setExtra({ ...extra, approve_repos: r })}
        />
      ) : null}
    </>
  );
}

function MockupBrief({ p, run }: { p: Record<string, any>; run: RunDetail }) {
  const b = p.design_brief;
  if (!b && !p.dev_notes) return null;
  const list = (title: string, items?: string[]) => (items?.length ? (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{title}</div>
      <ul className="list-disc pl-5 text-xs">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  ) : null);
  return (
    <Section title="Your mockups and notes, as the steps read them">
      <div className="space-y-2 rounded-lg border border-border bg-surface px-3 py-2" data-testid="design-brief">
        <Shots runId={run.run_id} paths={b?.images} />
        {p.dev_notes ? <p className="whitespace-pre-wrap text-xs"><span className="text-muted-foreground">Your notes: </span>{p.dev_notes}</p> : null}
        {b ? (
          <>
            <p className="text-xs">{b.summary}</p>
            {list("Screens", b.screens)}
            {list("Visible text", b.texts)}
            {list("Styling", b.styling)}
            {list("Interactions", b.interactions)}
            {b.ambiguities?.length ? <div className="text-xs text-warning">Not settled by the mockups: {b.ambiguities.join("; ")}</div> : null}
          </>
        ) : null}
      </div>
    </Section>
  );
}

function CodeView({ p }: { p: Record<string, any> }) {
  const reviews = Object.entries(p.reviews ?? {}) as [string, any][];
  return (
    <>
      <Section title="Open the Diff tab to read the changes, then decide">
        <div className="space-y-2">
          {Object.entries(p.repos ?? {}).map(([name, r]: [string, any]) => (
            <div key={name} className="rounded-lg border border-border bg-surface px-3 py-2" data-testid={`code-repo-${name}`}>
              <div className="flex flex-wrap items-center gap-2">
                <Mono className="text-foreground">{name}</Mono>
                <span className="text-xs text-muted-foreground">{(r.changed_files ?? []).length} files changed</span>
                <span className="text-xs text-muted-foreground">fix attempts {r.fix_attempts_used ?? 0}/3</span>
              </div>
              {r.changed_files?.length ? <div className="mt-1 font-mono text-[11px] text-muted-foreground">{r.changed_files.join("  ")}</div> : null}
              {r.checks ? <ChecksView checks={r.checks} /> : null}
            </div>
          ))}
        </div>
      </Section>
      {reviews.length ? (
        <Section title="Code review">
          <div className="space-y-2">
            {reviews.map(([repo, rv]) => (
              <div key={repo} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
                <div className="flex items-center gap-2">
                  <Mono className="text-foreground">{repo}</Mono>
                  <Pill tone={rv.decision === "approve" ? "success" : rv.decision === "comment" ? "warning" : "destructive"}>{humanize(rv.decision ?? "")}</Pill>
                  <span className="text-muted-foreground">{(rv.findings ?? []).length} findings</span>
                </div>
                {rv.summary ? <p className="mt-1 text-muted-foreground">{rv.summary}</p> : null}
                {(rv.findings ?? []).slice(0, 8).map((f: any, i: number) => (
                  <div key={i} className="mt-1"><Pill tone={f.severity === "blocker" || f.severity === "major" ? "destructive" : "muted"}>{f.severity}</Pill> <Mono>{f.file}{f.line ? `:${f.line}` : ""}</Mono> {f.title}</div>
                ))}
              </div>
            ))}
          </div>
        </Section>
      ) : null}
      {p.contract_review?.blockers?.length ? (
        <Section title="Cross-repo blockers"><IssueList items={p.contract_review.blockers} /></Section>
      ) : null}
    </>
  );
}

function PlanView({ p, run }: { p: Record<string, any>; run: RunDetail }) {
  const plan = p.plan ?? {};
  const dag = p.dag ?? {};
  return (
    <>
      <MockupBrief p={p} run={run} />
      {p.validation_errors?.length ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive-soft px-3 py-2 text-xs text-destructive">
          <div className="font-medium">The plan can't be approved yet</div>
          <ul className="mt-1 list-disc pl-5">
            {p.validation_errors.map((e: string, i: number) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={p.risk?.level === "high" ? "destructive" : p.risk?.level === "medium" ? "warning" : "success"}>Risk {p.risk?.level ?? "unknown"}</Pill>
        {(p.risk?.reasons ?? []).map((r: string, i: number) => (
          <span key={i} className="text-xs text-muted-foreground">
            {r}
          </span>
        ))}
      </div>
      {dag.waves?.length ? (
        <Section title="Repos and waves (each wave starts when the one before it is done)">
          <div className="flex flex-wrap items-center gap-2" data-testid="plan-waves">
            {dag.waves.map((w: string[], i: number) => (
              <div key={i} className="flex items-center gap-2">
                {i ? <span className="text-muted-foreground">→</span> : null}
                <div className="rounded-lg border border-border bg-surface px-2 py-1.5">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Wave {i + 1}</div>
                  <div className="flex gap-1">
                    {w.map((r) => (
                      <Mono key={r} className="rounded bg-muted px-1.5 text-foreground">
                        {r}
                      </Mono>
                    ))}
                  </div>
                </div>
              </div>
            ))}
          </div>
          {plan.edges?.length ? (
            <ul className="mt-2 space-y-0.5 text-xs text-muted-foreground">
              {plan.edges.map((e: any, i: number) => (
                <li key={i}>
                  <Mono>{e.upstream}</Mono> → <Mono>{e.downstream}</Mono>: {e.carries}
                </li>
              ))}
            </ul>
          ) : null}
        </Section>
      ) : null}
      <Section title="Tasks per repo">
        <div className="space-y-2">
          {(plan.repos ?? []).map((r: any) => (
            <div key={r.repo} className="rounded-lg border border-border bg-surface px-3 py-2">
              <Mono className="text-foreground">{r.repo}</Mono>
              <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-xs">
                {(r.tasks ?? []).map((t: string, i: number) => (
                  <li key={i}>{t}</li>
                ))}
              </ol>
              {r.likely_files?.length ? <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">{r.likely_files.join("  ")}</div> : null}
            </div>
          ))}
        </div>
      </Section>
      {[
        ["Contracts", plan.contracts],
        ["Migrations", plan.migrations],
        ["Risks", plan.risks],
        ["Warnings", p.warnings],
      ].map(([t, items]) =>
        (items as string[] | undefined)?.length ? (
          <Section key={t as string} title={t}>
            <ul className={cn("list-disc space-y-0.5 pl-5 text-xs", t === "Warnings" && "text-warning")}>
              {(items as string[]).map((x, i) => (
                <li key={i}>{x}</li>
              ))}
            </ul>
          </Section>
        ) : null,
      )}
      {plan.test_plan?.length ? (
        <Section title="Test plan">
          <ul className="space-y-1 text-xs">
            {plan.test_plan.map((t: any, i: number) => (
              <li key={i}>
                <span className="text-foreground">{t.criterion}</span>
                <span className="text-muted-foreground"> · {(t.tests ?? []).join("; ")}</span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </>
  );
}

function ManualTest({ p, run, extra, setExtra }: FormProps) {
  const repos = Object.entries(p.repos ?? {}) as [string, any][];
  return (
    <>
      <Section title="Test each repo from its own worktree">
        <div className="space-y-2">
          {repos.map(([name, r]) => (
            <div key={name} className="rounded-lg border border-border bg-surface px-3 py-2" data-testid={`manual-repo-${name}`}>
              <div className="flex flex-wrap items-center gap-2">
                <Mono className="text-foreground">{name}</Mono>
                <span className="text-xs text-muted-foreground">branch</span>
                <Mono>{r.branch}</Mono>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <Mono className="truncate text-muted-foreground">{r.path}</Mono>
                <CopyButton text={r.path} label="Copy path" />
                <a href={`vscode://file/${r.path}`} className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
                  <ExternalLink className="size-3" /> Open in VS Code
                </a>
              </div>
              {r.run ? (
                <div className="mt-1 flex items-center gap-2 text-xs">
                  <span className="text-muted-foreground">Run</span>
                  <Mono>{r.run}</Mono>
                  <CopyButton text={`cd ${r.path} && ${r.run}`} label="Copy" />
                </div>
              ) : null}
              {r.changed_files?.length ? <div className="mt-1 text-xs text-muted-foreground">{r.changed_files.length} changed: <Mono>{r.changed_files.slice(0, 6).join("  ")}</Mono></div> : null}
            </div>
          ))}
        </div>
      </Section>
      {p.mockups?.length ? (
        <Section title="Compare with the mockup(s)">
          <Shots runId={run.run_id} paths={p.mockups} />
        </Section>
      ) : null}
      {p.test_cases?.cases?.length ? (
        <Section title="Test cases, in the order to run them">
          <ol className="space-y-2" data-testid="test-cases">
            {p.test_cases.cases.map((c: any, i: number) => (
              <li key={i} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
                <div className="font-medium text-foreground">{i + 1}. {c.title}</div>
                <ol className="mt-1 list-decimal pl-5">{(c.steps ?? []).map((st: string, j: number) => <li key={j}>{st}</li>)}</ol>
                <div className="mt-1"><span className="text-muted-foreground">Expect: </span>{c.expected}</div>
                {c.covers ? <div className="text-muted-foreground">Covers: {c.covers}</div> : null}
              </li>
            ))}
          </ol>
        </Section>
      ) : null}
      {p.checklist?.length ? (
        <Section title="What to test">
          <ul className="space-y-1">
            {p.checklist.map((c: string, i: number) => (
              <li key={i} className="flex gap-2 text-xs">
                <input type="checkbox" className="mt-0.5 accent-[var(--success)]" aria-label={c} />
                <span>{c}</span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      {p.threads_fixed?.length ? (
        <Section title="Review threads fixed">
          <ul className="space-y-0.5 text-xs">
            {p.threads_fixed.map((t: any) => (
              <li key={t.n}>
                #{t.n} <Mono>{t.repo}</Mono> {t.summary}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      {p.integration && Object.keys(p.integration).length ? (
        <Section title="Integration results">
          <JsonTree value={p.integration} open={1} />
        </Section>
      ) : null}
      <RepoPicker label="Repos the feedback is about (if you know)" repos={repos.map(([n]) => n).length ? repos.map(([n]) => n) : Object.keys(run.scope ?? {})} value={(extra.repos as string[]) ?? []} onChange={(r) => setExtra({ ...extra, repos: r })} />
    </>
  );
}

function PushView({ p }: { p: Record<string, any> }) {
  return (
    <>
      <Section title="What will happen">
        <ul className="list-disc space-y-0.5 pl-5 text-xs">
          <li>
            Commit and push each repo's branch (never forced), in this order: <Mono className="text-foreground">{(p.merge_order ?? []).join(" → ")}</Mono>
          </li>
          <li>Open a draft MR per repo targeting develop (never merged by devflow)</li>
          {p.jira === false ? null : <li>Move the Jira ticket to Code Review and add one delivery comment</li>}
          <li>Manual test: {p.manual_test}</li>
        </ul>
      </Section>
      <Section title="Repos">
        <div className="space-y-2">
          {Object.entries(p.repos ?? {}).map(([name, r]: [string, any]) => (
            <div key={name} className="rounded-lg border border-border bg-surface px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <Mono className="text-foreground">{name}</Mono>
                <span className="text-xs text-muted-foreground">{(r.changed_files ?? []).length} files changed</span>
                <span className="text-xs text-muted-foreground">fix attempts {r.fix_attempts_used ?? 0}/3</span>
              </div>
              {r.changed_files?.length ? <div className="mt-1 font-mono text-[11px] text-muted-foreground">{r.changed_files.join("  ")}</div> : null}
              {r.checks ? <ChecksView checks={r.checks} /> : null}
            </div>
          ))}
        </div>
      </Section>
      {p.scope_gaps?.length ? (
        <Section title="Scope gaps (left for follow-up)">
          <IssueList items={p.scope_gaps} />
        </Section>
      ) : null}
      {p.reviews && Object.keys(p.reviews).length ? (
        <Section title="Reviews">
          <JsonTree value={p.reviews} open={1} />
        </Section>
      ) : null}
      {p.contract_review && Object.keys(p.contract_review).length ? (
        <Section title="Cross-repo review">
          <JsonTree value={p.contract_review} open={1} />
        </Section>
      ) : null}
    </>
  );
}

export function ChecksView({ checks }: { checks: any }) {
  if (!checks || typeof checks !== "object") return null;
  const entries = Object.entries(checks as Record<string, any>);
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {entries.map(([name, c]) => {
        const ok = typeof c === "object" ? c?.ok ?? c?.passed ?? c?.returncode === 0 : !!c;
        return (
          <span key={name} title={typeof c === "object" ? String(c?.tail ?? c?.output ?? "") : ""}>
            <Pill tone={ok ? "success" : "destructive"}>{name}</Pill>
          </span>
        );
      })}
    </div>
  );
}

function TriageForm({ p, extra, setExtra }: { p: Record<string, any>; extra: Answer; setExtra: (a: Answer) => void }) {
  const threads: any[] = p.threads ?? [];
  const [acts, setActs] = useState<Record<string, string>>(Object.fromEntries(threads.map((t) => [String(t.n), t.proposed_action])));
  const set = (n: string, a: string) => {
    const next = { ...acts, [n]: a };
    setActs(next);
    const changed = threads.some((t) => next[String(t.n)] !== t.proposed_action);
    const lists = { fix: [] as number[], answer: [] as number[], skip: [] as number[] };
    for (const t of threads) (lists as any)[next[String(t.n)]]?.push(t.n);
    setExtra(changed ? { ...extra, ...lists, _edited: true } : { note: extra.note });
  };
  return (
    <Section title={`Review threads (${threads.length})`} aside={extra._edited ? <span className="text-warning">Edited: use “Apply my triage”</span> : null}>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <thead className="bg-surface text-muted-foreground">
            <tr>
              <th className="px-2 py-1.5 text-left font-medium">#</th>
              <th className="px-2 py-1.5 text-left font-medium">Comment</th>
              <th className="px-2 py-1.5 text-left font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {threads.map((t) => (
              <tr key={t.n} className="border-t border-border align-top">
                <td className="px-2 py-2 font-mono">{t.n}</td>
                <td className="px-2 py-2">
                  <div className="text-foreground">{t.comment}</div>
                  <div className="mt-0.5 text-muted-foreground">
                    <Mono>
                      {t.repo}
                      {t.file ? ` · ${t.file}${t.line ? `:${t.line}` : ""}` : ""}
                    </Mono>{" "}
                    · {humanize(t.category ?? "")}
                  </div>
                  {t.summary ? <div className="mt-0.5 text-muted-foreground">{t.summary}</div> : null}
                  {t.reply ? <div className="mt-1 rounded bg-muted px-2 py-1 text-muted-foreground">Reply: {t.reply}</div> : null}
                </td>
                <td className="px-2 py-2">
                  <div className="inline-flex rounded-md border border-border p-0.5" role="group" aria-label={`Thread ${t.n} action`}>
                    {["fix", "answer", "skip"].map((a) => (
                      <button
                        key={a}
                        onClick={() => set(String(t.n), a)}
                        aria-pressed={acts[String(t.n)] === a}
                        className={cn("rounded px-2 py-0.5 capitalize", acts[String(t.n)] === a ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground")}
                      >
                        {a === "answer" ? "Answer only" : a}
                      </button>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function RepoPicker({ label, repos, value, onChange, hints }: { label: string; repos: string[]; value: string[]; onChange: (v: string[]) => void; hints?: Record<string, string> }) {
  if (!repos.length) return null;
  return (
    <div>
      <Label>{label}</Label>
      <div className="flex flex-wrap gap-1.5">
        {repos.map((r) => {
          const on = value.includes(r);
          return (
            <button
              key={r}
              title={hints?.[r]}
              aria-pressed={on}
              onClick={() => onChange(on ? value.filter((x) => x !== r) : [...value, r])}
              className={cn("rounded-md border px-2 py-0.5 font-mono text-xs", on ? "border-primary bg-primary-soft text-primary" : "border-border text-muted-foreground hover:text-foreground")}
            >
              {r}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Generic({ pc, onAnswer, busy }: { pc: Pending; onAnswer: (a: Answer) => void; busy: boolean }) {
  const [text, setText] = useState("");
  const payload = pc.payload as any;
  const question = typeof payload === "string" ? payload : payload?.question ?? payload?.message ?? payload?.prompt;
  return (
    <>
      {question ? <p className="text-sm">{question}</p> : null}
      {payload && typeof payload === "object" ? (
        <Section title="Payload">
          <JsonTree value={payload} open={2} />
        </Section>
      ) : null}
      <div>
        <Label htmlFor="generic-answer" hint="(plain text, or JSON)">
          Your answer
        </Label>
        <Textarea id="generic-answer" rows={3} value={text} onChange={(e) => setText(e.target.value)} data-testid="generic-answer" />
      </div>
      <div className="flex gap-2">
        <Button
          variant="success"
          disabled={busy || !text.trim()}
          onClick={() => {
            let v: unknown = text.trim();
            try {
              v = JSON.parse(text);
            } catch {
              /* plain text */
            }
            onAnswer({ value: v });
          }}
          data-testid="generic-submit"
        >
          <Check className="size-4" /> Send answer
        </Button>
      </div>
    </>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1200);
        });
      }}
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
    >
      {done ? <Check className="size-3 text-success" /> : <Copy className="size-3" />}
      {done ? "Copied" : label}
    </button>
  );
}
