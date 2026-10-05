import { Check, Minus, X } from "lucide-react";
import type { ReactNode } from "react";
import type { RunDetail } from "@/lib/api";
import { cn, duration } from "@/lib/format";
import { useNow } from "@/lib/hooks";
import type { Step, StepState } from "@/lib/steps";

export function Marker({ state, n, size = "md" }: { state: StepState; n: number | string; size?: "sm" | "md" }) {
  return (
    <span
      className={cn(
        "relative z-[1] inline-flex shrink-0 items-center justify-center rounded-lg border text-xs font-semibold",
        size === "md" ? "size-8" : "size-6 text-[11px]",
        state === "done" && "border-transparent bg-success-soft text-success",
        state === "running" && "animate-slow-pulse border-transparent bg-primary-soft text-primary",
        state === "waiting" && "border-transparent bg-warning-soft text-warning",
        state === "failed" && "border-transparent bg-destructive-soft text-destructive",
        state === "skipped" && "border-dashed border-border bg-surface text-muted-foreground",
        state === "pending" && "border-border bg-surface text-muted-foreground",
      )}
    >
      {state === "done" ? <Check className="size-4" /> : state === "failed" ? <X className="size-4" /> : state === "skipped" ? <Minus className="size-3.5" /> : n}
    </span>
  );
}

/** How many times a step ran; a checkpoint's waiting half is not counted again. */
export function runs(s: Step): number {
  return s.execs.filter((e) => !e.node.endsWith("_wait")).length || s.execs.length;
}

const STATE_TEXT: Partial<Record<StepState, ReactNode>> = {
  running: <span className="text-primary">In progress</span>,
  waiting: <span className="text-warning">Waiting</span>,
  failed: <span className="text-destructive">Failed</span>,
  skipped: <span className="text-muted-foreground">Skipped</span>,
};

export function Stepper({ steps, run, selected, onSelect, after }: {
  steps: Step[];
  run: RunDetail;
  selected?: string;
  onSelect: (step: Step) => void;
  after?: (step: Step) => ReactNode;
}) {
  const now = useNow(1000);
  return (
    <ol className="rounded-xl border border-border bg-surface p-2 sm:p-3" data-testid="stepper">
      {steps.map((s, i) => {
        const lineDone = s.state === "done";
        const repoStatus = run.repos ?? {};
        const live = s.state === "running" ? s.execs.filter((e) => e.end === undefined) : [];
        const dur = s.state === "running" ? live.reduce((t, e) => t + (now - e.start), 0) + s.execs.filter((e) => e.end).reduce((t, e) => t + (e.end! - e.start), 0) : s.duration;
        return (
          <li key={s.id} data-testid={`step-${s.id}`} data-state={s.state}>
            <button
              onClick={() => onSelect(s)}
              disabled={!s.execs.length && s.state !== "waiting"}
              className={cn(
                "group relative flex w-full items-start gap-3 rounded-lg px-2 py-2 text-left transition-colors duration-150 disabled:cursor-default",
                selected === s.id ? "bg-muted/70" : "enabled:hover:bg-muted/40",
              )}
            >
              {i < steps.length - 1 ? (
                <span className={cn("absolute left-[23px] top-10 bottom-[-8px] w-px", lineDone ? "bg-success/50" : "bg-track")} aria-hidden />
              ) : null}
              <Marker state={s.state} n={i + 1} />
              <div className="min-w-0 flex-1 pt-0.5">
                <div className="flex items-center gap-2">
                  <span className={cn("truncate text-sm font-medium", s.state === "pending" || s.state === "skipped" ? "text-muted-foreground" : "text-foreground")}>{s.title}</span>
                  {runs(s) > 1 ? <span className="rounded bg-muted px-1 text-[11px] text-muted-foreground">×{runs(s)}</span> : null}
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  {s.repos.length > 1 || (s.repos.length === 1 && runs(s) > 1) ? (
                    s.repos.map((r) => {
                      const running = live.some((e) => e.repo === r);
                      const st = repoStatus[r];
                      return (
                        <span
                          key={r}
                          className={cn(
                            "rounded px-1.5 py-px font-mono text-[11px]",
                            running ? "bg-primary-soft text-primary" : st?.status === "ready" || st?.implemented ? "bg-success-soft text-success" : "bg-muted text-muted-foreground",
                          )}
                        >
                          {r}
                          {st?.fix_attempts_used ? ` ${st.fix_attempts_used}/3` : ""}
                        </span>
                      );
                    })
                  ) : null}
                  <span className="truncate">
                    {s.state === "failed" && run.status === "FAILED" ? <span className="text-destructive">{run.detail}</span> : s.summary || s.detail}
                  </span>
                </div>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-0.5 pt-0.5 text-xs">
                {STATE_TEXT[s.state]}
                {dur >= 1 ? <span className="font-mono text-[11px] text-muted-foreground">{duration(dur)}</span> : null}
              </div>
            </button>
            {after?.(s)}
          </li>
        );
      })}
      {!steps.length ? <li className="p-4 text-sm text-muted-foreground">No steps recorded yet.</li> : null}
    </ol>
  );
}
