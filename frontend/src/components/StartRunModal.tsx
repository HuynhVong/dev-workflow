import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, type FormField } from "@/lib/api";
import { cn } from "@/lib/format";
import { useMeta, useWorkflows } from "@/lib/hooks";
import { ImagesField } from "./ImagesField";
import { Button, ErrorNote, Input, Label, Modal, Pill, Select, Spinner, Textarea } from "./ui";

export function StartRunModal({ open, initialWorkflow, onClose, onStarted }: {
  open: boolean;
  initialWorkflow?: string;
  onClose: () => void;
  onStarted: (runId: string) => void;
}) {
  const wfs = useWorkflows(open).data?.workflows ?? [];
  const meta = useMeta().data;
  const [wid, setWid] = useState(initialWorkflow ?? "");
  const [values, setValues] = useState<Record<string, unknown>>({});
  const qc = useQueryClient();

  useEffect(() => {
    if (open) {
      setWid(initialWorkflow || wfs[0]?.id || "");
      setValues({});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialWorkflow]);
  useEffect(() => {
    if (open && !wid && wfs.length) setWid(wfs[0].id);
  }, [open, wid, wfs]);

  const wf = wfs.find((w) => w.id === wid);
  const fields = useMemo(() => (wf?.form ?? []).filter((f) => !(wf?.generated_form && ["output"].includes(f.name))), [wf]);

  useEffect(() => {
    const init: Record<string, unknown> = {};
    for (const f of fields) if (f.default !== undefined) init[f.name] = f.default;
    setValues(init);
  }, [wid, fields]);

  // The ticket review names its repos in the commit lines ("web-portal=3f9a1c2"); the other ticket workflows pick them.
  const repos = Array.isArray(values.commits)
    ? [...new Set((values.commits as string[]).map((l) => l.split("=")[0].trim()).filter((r) => meta?.repos.includes(r)))]
    : ((values.repos as string[] | undefined) ?? []);
  const preflight = useQuery({
    queryKey: ["preflight", wid, repos.join(",")],
    queryFn: () => api.runDoctor({ repos: repos.length ? repos : meta?.repos, workflow: wid }),
    enabled: open && wf?.kind === "ticket" && !!meta?.loaded,
    staleTime: 30_000,
  });
  const blocking = blockingChecks(preflight.data?.checks);
  const missing = fields.filter((f) => f.required && isEmpty(values[f.name]));
  const full = meta ? meta.slots.running >= meta.slots.max : false;

  const start = useMutation({
    mutationFn: () => api.startRun(wid, clean(values, fields)),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["meta"] });
      onStarted(r.run_id);
    },
  });
  let jsonError = "";
  try {
    clean(values, fields);
  } catch (e) {
    jsonError = (e as Error).message;
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Start a new run"
      subtitle="Launch another workflow alongside your active runs."
      footer={
        <>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            data-testid="start-run"
            disabled={!wf || !!missing.length || !!blocking.length || !!jsonError || start.isPending || (wf.kind === "ticket" && preflight.isLoading)}
            onClick={() => start.mutate()}
          >
            {start.isPending ? <Spinner /> : <Play className="size-3.5" />}
            {full ? "Queue run" : wf?.kind === "ticket" ? "Check & start" : "Start run"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div>
          <Label htmlFor="wf">Workflow</Label>
          <div className="relative">
            <span className="pointer-events-none absolute left-3 top-1/2 size-2 -translate-y-1/2 rounded-full" style={{ background: wf?.color }} />
            <Select id="wf" className="pl-7" value={wid} onChange={(e) => setWid(e.target.value)} data-testid="workflow-select">
              {wfs.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.title}
                </option>
              ))}
            </Select>
          </div>
          {wf?.description ? <p className="mt-1.5 text-xs text-muted-foreground">{wf.description}</p> : null}
          {wf?.error ? <ErrorNote error={`This workflow failed to load: ${wf.error}`} /> : null}
        </div>
        {wf?.generated_form ? (
          <p className="text-xs text-muted-foreground">This form was generated from the graph's input schema. Leave fields the graph fills itself empty.</p>
        ) : null}
        {fields.map((f) => (
          <Field key={f.name} field={f} value={values[f.name]} onChange={(v) => setValues((cur) => ({ ...cur, [f.name]: v }))} repos={meta?.repos ?? []} />
        ))}
        {wf && !wf.generated_form && wf.kind === "graph" ? (
          <div>
            <Label htmlFor="label" hint="(optional)">
              Run title
            </Label>
            <Input id="label" value={(values._label as string) ?? ""} onChange={(e) => setValues((cur) => ({ ...cur, _label: e.target.value }))} placeholder="Shown in the runs list" />
          </div>
        ) : null}
        {wf?.kind === "ticket" ? <Preflight loading={preflight.isFetching} checks={preflight.data?.checks} error={preflight.error} /> : null}
        {full ? (
          <p className="text-xs text-muted-foreground">
            All {meta?.slots.max} parallel slots are busy, so this run waits in the queue and starts when one frees up.
          </p>
        ) : null}
        <ErrorNote error={jsonError || start.error} />
      </div>
    </Modal>
  );
}

type PreCheck = { id: string; group: string; label: string; status: string; blocking: boolean; detail: string; fix: string };

/** The groups the graph's own preflight node checks; only their blocking failures stop a run from starting here. */
const PREFLIGHT_GROUPS = ["CLIs", "Jira MCP", "Confluence MCP", "Playwright MCP", "Repos"];

export function blockingChecks(checks?: PreCheck[]): PreCheck[] {
  return (checks ?? []).filter((c) => c.status === "fail" && c.blocking && PREFLIGHT_GROUPS.includes(c.group));
}

function Preflight({ loading, checks, error }: { loading: boolean; checks?: PreCheck[]; error: unknown }) {
  if (error) return <ErrorNote error={error} />;
  const all = checks ?? [];
  const blocking = blockingChecks(all);
  const failing = all.filter((c) => c.status === "fail" && !blocking.includes(c));
  const warns = all.filter((c) => c.status === "warn");
  const key = all.filter((c) => c.status === "ok" && (c.id.startsWith("repo.") || c.id.startsWith("mcp.")));
  return (
    <div className="rounded-lg border border-border bg-surface p-3" data-testid="preflight">
      <div className="mb-2 flex items-center gap-2 text-xs font-medium">
        Preflight {loading ? <Spinner className="text-muted-foreground" /> : null}
        {!loading && checks ? (
          <span className="font-normal text-muted-foreground">
            {all.filter((c) => c.status === "ok").length} of {all.length} checks OK
          </span>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {[...blocking, ...failing].map((c) => (
          <span key={c.id} title={`${c.detail}${c.fix ? `\nHow to fix: ${c.fix}` : ""}`}>
            <Pill tone={blocking.includes(c) ? "destructive" : "warning"}>{c.label}</Pill>
          </span>
        ))}
        {key.map((c) => (
          <span key={c.id} title={c.detail}>
            <Pill tone="success">{c.label}</Pill>
          </span>
        ))}
        {warns.length ? (
          <span title={warns.map((c) => `${c.label}: ${c.detail}`).join("\n")}>
            <Pill tone="warning">
              {warns.length} warning{warns.length === 1 ? "" : "s"}
            </Pill>
          </span>
        ) : null}
      </div>
      {blocking.map((c) => (
        <p key={c.id} className="mt-2 text-xs text-destructive">
          {c.label}: {c.detail}
          {c.fix ? <span className="text-muted-foreground"> {c.fix}</span> : null}
        </p>
      ))}
      {failing.length ? <p className="mt-2 text-xs text-muted-foreground">{failing.map((c) => c.label).join(", ")} failed; the run can start, but steps that need it will fail until it is fixed.</p> : null}
    </div>
  );
}

function Field({ field: f, value, onChange, repos }: { field: FormField; value: unknown; onChange: (v: unknown) => void; repos: string[] }) {
  const id = `f-${f.name}`;
  const label = (
    <Label htmlFor={id} hint={f.required ? undefined : f.type === "repos" ? undefined : "(optional)"}>
      {f.label}
    </Label>
  );
  const help = f.help ? <p className="mt-1 text-xs text-muted-foreground">{f.help}</p> : null;
  if (f.type === "repos") {
    const sel = (value as string[] | undefined) ?? [];
    return (
      <div>
        {label}
        <div className="flex flex-wrap gap-1.5" data-testid="repos-picker">
          {repos.map((r) => {
            const on = sel.includes(r);
            return (
              <button
                key={r}
                type="button"
                aria-pressed={on}
                onClick={() => onChange(on ? sel.filter((x) => x !== r) : [...sel, r])}
                className={cn(
                  "rounded-md border px-2.5 py-1 font-mono text-xs transition-colors duration-150",
                  on ? "border-primary bg-primary-soft text-primary" : "border-border text-muted-foreground hover:text-foreground",
                )}
              >
                {r}
              </button>
            );
          })}
          {!repos.length ? <span className="text-xs text-muted-foreground">No repos in workspace.yaml yet.</span> : null}
        </div>
        {help}
      </div>
    );
  }
  if (f.type === "images") {
    return (
      <div>
        {label}
        <ImagesField value={(value as string[] | undefined) ?? []} onChange={onChange} />
        {help}
      </div>
    );
  }
  if (f.type === "bool") {
    return (
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} className="accent-[var(--primary)]" />
        {f.label}
      </label>
    );
  }
  if (f.type === "select") {
    return (
      <div>
        {label}
        <Select id={id} value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose…</option>
          {(f.options ?? []).map((o) => (
            <option key={o}>{o}</option>
          ))}
        </Select>
        {help}
      </div>
    );
  }
  if (f.type === "textarea" || f.type === "json" || f.type === "list") {
    const text = f.type === "list" && Array.isArray(value) ? value.join("\n") : ((value as string) ?? "");
    return (
      <div>
        {label}
        <Textarea
          id={id}
          value={text}
          rows={f.rows ?? (f.name === "diff" || f.name === "template" ? 8 : 3)}
          className={cn((f.type === "json" || (f as any).mono) && "font-mono text-xs")}
          placeholder={f.placeholder ?? (f.type === "list" ? "One item per line" : f.type === "json" ? "{ }" : "")}
          onChange={(e) => onChange(f.type === "list" ? e.target.value.split("\n") : e.target.value)}
        />
        {help}
      </div>
    );
  }
  return (
    <div>
      {label}
      <Input
        id={id}
        type={f.type === "number" ? "number" : f.type === "date" ? "date" : "text"}
        value={(value as string) ?? ""}
        placeholder={f.placeholder}
        className={cn(f.type === "ticket" && "font-mono uppercase", f.mono && "font-mono")}
        autoFocus={f.type === "ticket"}
        onChange={(e) => onChange(f.type === "ticket" ? e.target.value.toUpperCase() : e.target.value)}
      />
      {help}
    </div>
  );
}

function isEmpty(v: unknown) {
  return v === undefined || v === null || (typeof v === "string" && !v.trim()) || (Array.isArray(v) && !v.filter(Boolean).length);
}

function clean(values: Record<string, unknown>, fields: FormField[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(values)) {
    const f = fields.find((x) => x.name === k);
    if (isEmpty(v)) continue;
    if (f?.type === "json") {
      try {
        out[k] = JSON.parse(v as string);
      } catch {
        throw new Error(`${f.label} is not valid JSON`);
      }
    } else if (f?.type === "number") out[k] = Number(v);
    else if (f?.type === "list") out[k] = (v as string[]).map((x) => x.trim()).filter(Boolean);
    else out[k] = typeof v === "string" ? v.trim() : v;
  }
  return out;
}

