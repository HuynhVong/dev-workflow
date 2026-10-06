// Approval forms for the standup checkpoints: confirm the tickets Jira's history found, then review the report.
import { useState } from "react";
import { cn } from "@/lib/format";
import { Mono, Pill, Textarea } from "../ui";
import { CopyButton } from "./Approval";

type Answer = Record<string, unknown> & { choice?: string };
type Props = { p: Record<string, any>; extra: Answer; setExtra: (a: Answer) => void };
type Ticket = { key: string; summary: string; status: string; group: "started" | "reviewed"; at: string; url: string; comments: number };

const GROUPS: { id: Ticket["group"]; title: (s: Record<string, string>) => string }[] = [
  { id: "started", title: (s) => `Started (${s.started_from} → ${s.started_to}, assigned to you)` },
  { id: "reviewed", title: (s) => `Reviewed (moved to ${s.review_status} while assigned to you)` },
];

export function ConfirmTicketsForm({ p, extra, setExtra }: Props) {
  const tickets = (p.tickets ?? []) as Ticket[];
  const [keep, setKeep] = useState<string[]>(tickets.map((t) => t.key));
  const toggle = (key: string, on: boolean) => {
    const next = on ? [...keep, key] : keep.filter((k) => k !== key);
    setKeep(next);
    setExtra({ ...extra, keep: next });
  };
  return (
    <>
      <div className="text-xs text-muted-foreground">
        {p.range?.from} → {p.range?.to} ({p.range?.timezone}) for <span className="text-foreground">{p.me}</span> · {keep.length} of {tickets.length} ticked
      </div>
      {p.warnings?.length ? (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-warning">
          {p.warnings.map((w: string, i: number) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      ) : null}
      {GROUPS.map((g) => {
        const rows = tickets.filter((t) => t.group === g.id);
        return (
          <div key={g.id}>
            <div className="mb-1.5 text-xs font-medium text-muted-foreground">{g.title(p.statuses ?? {})}</div>
            {rows.length ? (
              <ul className="space-y-1.5" data-testid={`tickets-${g.id}`}>
                {rows.map((t) => (
                  <li key={t.key} className="flex items-start gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-xs">
                    <input
                      type="checkbox"
                      className="mt-0.5 accent-[var(--success)]"
                      checked={keep.includes(t.key)}
                      onChange={(e) => toggle(t.key, e.target.checked)}
                      aria-label={`Keep ${t.key}`}
                      data-testid={`ticket-${t.key}`}
                    />
                    <div className={cn("min-w-0 flex-1", !keep.includes(t.key) && "opacity-50")}>
                      {t.url ? (
                        <a href={t.url} target="_blank" rel="noreferrer" className="font-mono text-primary hover:underline">
                          {t.key}
                        </a>
                      ) : (
                        <Mono className="text-foreground">{t.key}</Mono>
                      )}{" "}
                      <span className="text-foreground">{t.summary}</span>
                      <div className="mt-0.5 text-muted-foreground">
                        {t.at.replace("T", " ")} · now {t.status}
                        {t.comments ? ` · ${t.comments} comment(s) by you` : ""}
                      </div>
                    </div>
                    <Pill tone={g.id === "started" ? "primary" : "success"}>{g.id}</Pill>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">None in this range.</p>
            )}
          </div>
        );
      })}
    </>
  );
}

export function ReportForm({ p, extra, setExtra }: Props) {
  const text = (extra.report as string) ?? p.report ?? "";
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground">
        <span>Report ({p.tickets ?? 0} ticket(s)). Edit it here, then Use my edits, or Save as is.</span>
        <CopyButton text={text} label="Copy report" />
      </div>
      <Textarea
        rows={16}
        className="font-mono text-xs"
        value={text}
        onChange={(e) => setExtra({ ...extra, report: e.target.value, _edited: e.target.value !== p.report })}
        data-testid="standup-report"
      />
    </div>
  );
}
