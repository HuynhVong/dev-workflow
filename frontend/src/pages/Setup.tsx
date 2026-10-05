import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Check, Lock, Play } from "lucide-react";
import { useState } from "react";
import { Connections } from "@/components/Connections";
import { Marker } from "@/components/run/Stepper";
import { Button, Card, Pill } from "@/components/ui";
import { WorkspaceEditor } from "@/components/WorkspaceEditor";
import { api } from "@/lib/api";
import { useMeta } from "@/lib/hooks";
import type { StepState } from "@/lib/steps";

const STEPS = ["Workspace", "Connections", "Skills and models", "Done"];

export function Setup({ onStartRun }: { onStartRun: () => void }) {
  const meta = useMeta().data;
  const [step, setStep] = useState(0);
  const loaded = !!meta?.loaded;
  const doctor = useQuery({ queryKey: ["doctor"], queryFn: api.doctor, enabled: loaded && step >= 1, staleTime: 5 * 60_000 });

  const state = (i: number): StepState => (i < step ? "done" : i === step ? "running" : "pending");
  return (
    <div className="mx-auto max-w-[720px]">
      <div className="mb-6">
        <div className="mb-2 text-[11px] font-medium uppercase tracking-widest text-primary">First-time setup</div>
        <h1 className="text-[28px] font-bold tracking-tight">Set up devflow</h1>
        <p className="mt-1 text-sm text-muted-foreground">Every check is read only. Nothing is created in Jira, GitLab or Confluence during setup.</p>
      </div>
      <ol className="mb-6 flex flex-wrap items-center gap-3" data-testid="setup-steps">
        {STEPS.map((s, i) => (
          <li key={s} className="flex items-center gap-2">
            <button onClick={() => (i === 0 || loaded) && setStep(i)} className="flex items-center gap-2 disabled:cursor-default" disabled={i > 0 && !loaded}>
              <span className={state(i) === "running" ? "[&>span]:animate-none" : ""}>
                <Marker state={state(i)} n={i + 1} size="sm" />
              </span>
              <span className={i === step ? "text-sm font-medium" : "text-sm text-muted-foreground"}>{s}</span>
            </button>
            {i < STEPS.length - 1 ? <span className="h-px w-6 bg-track" /> : null}
          </li>
        ))}
      </ol>

      {step === 0 ? (
        <>
          {meta?.load_error && meta.workspace_exists ? <p className="mb-3 text-xs text-destructive">The current workspace.yaml does not load: {meta.load_error}</p> : null}
          <WorkspaceEditor saveLabel="Save and continue" onSaved={() => setStep(1)} />
        </>
      ) : null}

      {step === 1 ? (
        <>
          <Connections compact />
          <div className="mt-5 flex justify-end">
            <Button variant="primary" onClick={() => setStep(2)}>
              Continue <ArrowRight className="size-4" />
            </Button>
          </div>
        </>
      ) : null}

      {step === 2 ? (
        <>
          <SkillsTable />
          <div className="mt-5 flex justify-end">
            <Button variant="primary" onClick={() => setStep(3)}>
              Continue <ArrowRight className="size-4" />
            </Button>
          </div>
        </>
      ) : null}

      {step === 3 ? (
        <Card className="p-6 text-center">
          <div className="mx-auto mb-3 flex size-10 items-center justify-center rounded-xl bg-success-soft text-success">
            <Check className="size-5" />
          </div>
          <h2 className="text-lg font-semibold">You're set up</h2>
          <div className="mt-2 flex justify-center gap-2">
            {doctor.data ? (
              <>
                <Pill tone={doctor.data.summary.blocking ? "destructive" : "success"}>{doctor.data.summary.blocking} blocking</Pill>
                <Pill tone={doctor.data.summary.warn ? "warning" : "success"}>{doctor.data.summary.warn} warnings</Pill>
              </>
            ) : null}
          </div>
          <p className="mx-auto mt-2 max-w-md text-sm text-muted-foreground">
            {doctor.data?.summary.blocking ? "Fix the blocking items in Connections before a ticket run; preflight checks them again anyway." : "Warnings never block a run. You can reopen these checks any time from Settings."}
          </p>
          <Button variant="primary" className="mt-4" onClick={onStartRun} data-testid="first-run">
            <Play className="size-4" /> Start your first run
          </Button>
        </Card>
      ) : null}
    </div>
  );
}

export function SkillsTable() {
  const q = useQuery({ queryKey: ["skills"], queryFn: api.skills });
  const d = q.data;
  if (q.error) return <p className="text-xs text-destructive">{(q.error as Error).message}</p>;
  if (!d) return null;
  return (
    <Card className="overflow-x-auto" data-testid="skills-table">
      <table className="w-full text-sm">
        <thead className="text-xs text-muted-foreground">
          <tr className="border-b border-border">
            {["Step", "Model", "Effort", "Skills"].map((h) => (
              <th key={h} className="px-4 py-2.5 text-left font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {d.steps.map((s) => (
            <tr key={s.step} className="border-b border-border/60 last:border-0">
              <td className="px-4 py-2 font-mono text-[12px]">{s.step}</td>
              <td className="px-4 py-2 text-xs">
                <span className="capitalize">{s.tier}</span> <span className="font-mono text-muted-foreground">{s.model}</span>
              </td>
              <td className="px-4 py-2 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <Lock className="size-3" /> medium
                </span>
              </td>
              <td className="px-4 py-2">
                <div className="flex flex-wrap gap-1">
                  {s.skills.map((k) => (
                    <Pill key={k} tone={s.missing.includes(k) ? "warning" : "success"}>
                      {k}
                      {s.missing.includes(k) ? " (missing)" : ""}
                    </Pill>
                  ))}
                  {!s.skills.length ? <span className="text-xs text-muted-foreground">none</span> : null}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-4 py-3 text-xs text-muted-foreground">Missing skills are warnings, never blockers. Change a step's model or skills in Settings.</p>
    </Card>
  );
}
