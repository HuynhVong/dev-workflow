import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, RefreshCw, X } from "lucide-react";
import { api, type Check as CheckT, type DoctorResult } from "@/lib/api";
import { ago } from "@/lib/format";
import { Button, Card, ErrorNote, IconSquare, Pill, Spinner } from "./ui";

const GROUPS = ["AI", "CLIs", "Jira MCP", "Confluence MCP", "Playwright MCP", "Repos", "Skills", "Optional"];

/** Every check the doctor runs, read only: nothing is created in Jira, GitLab or Confluence. */
export function Connections({ compact }: { compact?: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["doctor"], queryFn: api.doctor, staleTime: 5 * 60_000 });
  const run = useMutation({
    mutationFn: (ping: boolean) => api.runDoctor({ ping_models: ping }),
    onSuccess: (d) => {
      qc.setQueryData<DoctorResult>(["doctor"], d);
      qc.invalidateQueries({ queryKey: ["meta"] });
    },
  });
  const d = run.data ?? q.data;
  const groups = [...GROUPS, ...new Set((d?.checks ?? []).map((c) => c.group).filter((g) => !GROUPS.includes(g)))];
  return (
    <div className="space-y-4" data-testid="connections">
      <div className="flex flex-wrap items-center gap-2">
        {d ? (
          <>
            <Pill tone="success">{d.summary.ok} OK</Pill>
            {d.summary.warn ? <Pill tone="warning">{d.summary.warn} warnings</Pill> : null}
            {d.summary.fail ? <Pill tone="destructive">{d.summary.fail} failed{d.summary.blocking ? `, ${d.summary.blocking} blocking` : ""}</Pill> : null}
            <span className="text-xs text-muted-foreground">checked {ago(d.at)}</span>
          </>
        ) : null}
        <div className="ml-auto flex gap-2">
          <Button size="sm" variant="ghost" disabled={run.isPending} onClick={() => run.mutate(true)} title="One tiny call per model tier; costs a few tokens">
            Ping models
          </Button>
          <Button size="sm" disabled={run.isPending || q.isFetching} onClick={() => run.mutate(false)} data-testid="recheck">
            {run.isPending ? <Spinner /> : <RefreshCw className="size-3.5" />} Re-check
          </Button>
        </div>
      </div>
      <ErrorNote error={q.error || run.error} />
      {!d ? <Spinner className="text-muted-foreground" /> : null}
      {groups.map((g) => {
        const checks = (d?.checks ?? []).filter((c) => c.group === g);
        if (!checks.length) return null;
        return (
          <div key={g}>
            <div className="mb-2 text-[11px] font-medium uppercase tracking-widest text-muted-foreground">{g}</div>
            <div className={compact ? "grid gap-2" : "grid gap-3 md:grid-cols-2"}>
              {checks.map((c) => (
                <CheckCard key={c.id} c={c} />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CheckCard({ c }: { c: CheckT }) {
  const tone = c.status === "ok" ? "success" : c.status === "warn" || !c.blocking ? "warning" : "destructive";
  return (
    <Card className="flex gap-3 p-3.5" data-testid={`check-${c.id}`}>
      <IconSquare tone={tone}>{c.status === "ok" ? <Check className="size-4" /> : c.status === "warn" ? <AlertTriangle className="size-4" /> : <X className="size-4" />}</IconSquare>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <span className="text-sm font-medium">{c.label}</span>
          <Pill tone={tone}>{c.status === "ok" ? "OK" : c.status === "warn" ? "Warning" : c.blocking ? "Blocking" : "Failed"}</Pill>
        </div>
        {c.detail ? <p className="mt-0.5 break-words text-xs text-muted-foreground">{c.detail}</p> : null}
        {c.fix && c.status !== "ok" ? <p className="mt-1 break-words text-xs text-foreground/80">How to fix: {c.fix}</p> : null}
      </div>
    </Card>
  );
}
