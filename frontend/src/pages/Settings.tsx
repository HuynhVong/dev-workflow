import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Cable, Check, Coins, Cpu, FileCog, Lock, RefreshCw } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useParams } from "react-router-dom";
import { Connections } from "@/components/Connections";
import { Button, Card, ErrorNote, Input, Label, Pill, Select, Spinner } from "@/components/ui";
import { WorkspaceEditor } from "@/components/WorkspaceEditor";
import { api } from "@/lib/api";
import { cn } from "@/lib/format";
import { useLocal } from "@/lib/hooks";

const SECTIONS: { id: string; label: string; icon: ReactNode }[] = [
  { id: "workspace", label: "Workspace", icon: <FileCog className="size-4" /> },
  { id: "connections", label: "Connections", icon: <Cable className="size-4" /> },
  { id: "models", label: "Models and skills", icon: <Cpu className="size-4" /> },
  { id: "prices", label: "Prices", icon: <Coins className="size-4" /> },
  { id: "app", label: "App", icon: <Bell className="size-4" /> },
];

export function Settings() {
  const { section = "workspace" } = useParams();
  return (
    <div className="mx-auto max-w-[1200px]">
      <h1 className="mb-1 text-[28px] font-bold tracking-tight">Settings</h1>
      <p className="mb-6 text-sm text-muted-foreground">Everything here is saved to your workspace.yaml, except app preferences, which stay in this browser.</p>
      <div className="grid gap-6 lg:grid-cols-[200px_1fr]">
        <nav className="flex gap-1 overflow-x-auto lg:flex-col">
          {SECTIONS.map((s) => (
            <NavLink
              key={s.id}
              to={`/settings/${s.id}`}
              className={() =>
                cn(
                  "flex items-center gap-2 whitespace-nowrap rounded-lg px-3 py-2 text-sm transition-colors duration-150",
                  section === s.id ? "bg-raised text-foreground" : "text-muted-foreground hover:bg-raised/60 hover:text-foreground",
                )
              }
            >
              {s.icon} {s.label}
            </NavLink>
          ))}
        </nav>
        <div className="min-w-0">
          {section === "workspace" ? <WorkspaceEditor /> : null}
          {section === "connections" ? <Connections /> : null}
          {section === "models" ? <ModelsAndSkills /> : null}
          {section === "prices" ? <Prices /> : null}
          {section === "app" ? <AppPrefs /> : null}
        </div>
      </div>
    </div>
  );
}

/** Reads workspace.yaml, lets a section change one part of it, and saves the whole (secrets stay masked). */
function useWorkspacePatch() {
  const qc = useQueryClient();
  const ws = useQuery({ queryKey: ["workspace"], queryFn: api.workspace });
  const save = useMutation({
    mutationFn: (patch: (d: Record<string, any>) => Record<string, any>) => api.saveWorkspace(patch(structuredClone(ws.data!.data))),
    onSuccess: () => qc.invalidateQueries(),
  });
  return { ws, save };
}

function ModelsAndSkills() {
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.skills });
  const { ws, save } = useWorkspacePatch();
  const [draft, setDraft] = useState<Record<string, { model: string; skills: string[] }>>({});
  useEffect(() => {
    if (skills.data) setDraft(Object.fromEntries(skills.data.steps.map((s) => [s.step, { model: s.tier, skills: s.skills }])));
  }, [skills.data]);
  const d = skills.data;
  if (!d || !ws.data) return <Spinner className="text-muted-foreground" />;
  const tiers = Object.keys(d.models);
  const installed = d.installed.map((s) => s.name);
  const changed = d.steps.filter((s) => draft[s.step] && (draft[s.step].model !== s.tier || draft[s.step].skills.join(",") !== s.skills.join(",")));
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Effort is medium for every step and cannot be changed. Skills are global Claude Code skills found in {d.dirs.join(", ") || "your skill folders"}; a missing skill is a warning, never a blocker.
      </p>
      <Card className="overflow-x-auto">
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
            {d.steps.map((s) => {
              const cur = draft[s.step] ?? { model: s.tier, skills: s.skills };
              return (
                <tr key={s.step} className="border-b border-border/60 align-top last:border-0">
                  <td className="px-4 py-2.5 font-mono text-[12px]">{s.step}</td>
                  <td className="px-4 py-2">
                    <Select className="h-8 w-36 text-xs" value={cur.model} onChange={(e) => setDraft({ ...draft, [s.step]: { ...cur, model: e.target.value } })} aria-label={`${s.step} model`}>
                      {[...new Set([...tiers, cur.model])].map((t) => (
                        <option key={t} value={t}>
                          {t} {d.models[t] ? `(${d.models[t]})` : ""}
                        </option>
                      ))}
                    </Select>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-muted-foreground">
                    <span className="inline-flex items-center gap-1">
                      <Lock className="size-3" /> medium
                    </span>
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex flex-wrap items-center gap-1">
                      {cur.skills.map((k) => (
                        <button key={k} onClick={() => setDraft({ ...draft, [s.step]: { ...cur, skills: cur.skills.filter((x) => x !== k) } })} title="Remove">
                          <Pill tone={installed.includes(k) ? "success" : "warning"}>
                            {k}
                            {installed.includes(k) ? "" : " (missing)"} ×
                          </Pill>
                        </button>
                      ))}
                      <Select
                        className="h-7 w-32 text-xs"
                        value=""
                        onChange={(e) => e.target.value && setDraft({ ...draft, [s.step]: { ...cur, skills: [...cur.skills, e.target.value] } })}
                        aria-label={`Add skill to ${s.step}`}
                      >
                        <option value="">+ skill</option>
                        {installed.filter((k) => !cur.skills.includes(k)).map((k) => (
                          <option key={k}>{k}</option>
                        ))}
                      </Select>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          disabled={!changed.length || save.isPending}
          onClick={() =>
            save.mutate((w) => {
              w.ai ??= {};
              w.ai.steps ??= {};
              for (const s of changed) w.ai.steps[s.step] = { ...(w.ai.steps[s.step] ?? {}), model: draft[s.step].model, skills: draft[s.step].skills };
              return w;
            })
          }
        >
          {save.isPending ? <Spinner /> : <Check className="size-4" />} Save {changed.length ? `${changed.length} change${changed.length === 1 ? "" : "s"}` : ""}
        </Button>
        {save.isSuccess ? <span className="text-xs text-success">Saved to ai.steps in workspace.yaml.</span> : null}
      </div>
      <ErrorNote error={save.error || skills.error} />
    </div>
  );
}

type Price = { input?: number; output?: number; cache_write?: number; cache_read?: number };

function Prices() {
  const { ws, save } = useWorkspacePatch();
  const [rows, setRows] = useState<[string, Price][] | null>(null);
  useEffect(() => {
    if (ws.data && rows === null) setRows(Object.entries((ws.data.data.prices ?? {}) as Record<string, Price>));
  }, [ws.data, rows]);
  if (!rows) return <Spinner className="text-muted-foreground" />;
  const set = (i: number, k: keyof Price | "model", v: string) => {
    const next = [...rows];
    if (k === "model") next[i] = [v, next[i][1]];
    else next[i] = [next[i][0], { ...next[i][1], [k]: v === "" ? undefined : Number(v) }];
    setRows(next);
  };
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        USD per million tokens, used to estimate cost on the Tokens pages. Claude Code sessions report their own cost. A key matches a model id exactly or as its prefix. Check these against current Anthropic pricing; tokens are always shown, even with no price.
      </p>
      <Card className="overflow-x-auto p-1">
        <table className="w-full text-sm" data-testid="prices">
          <thead className="text-xs text-muted-foreground">
            <tr>
              {["Model (id or prefix)", "Input", "Output", "Cache write", "Cache read", ""].map((h) => (
                <th key={h} className="px-3 py-2 text-left font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(([m, p], i) => (
              <tr key={i}>
                <td className="px-2 py-1">
                  <Input className="h-8 font-mono text-xs" value={m} onChange={(e) => set(i, "model", e.target.value)} aria-label="Model" />
                </td>
                {(["input", "output", "cache_write", "cache_read"] as const).map((k) => (
                  <td key={k} className="px-2 py-1">
                    <Input className="h-8 w-24 font-mono text-xs" type="number" step="0.01" value={p[k] ?? ""} onChange={(e) => set(i, k, e.target.value)} aria-label={k} />
                  </td>
                ))}
                <td className="px-2 py-1">
                  <Button size="sm" variant="ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                    Remove
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <div className="flex gap-2">
        <Button size="sm" onClick={() => setRows([...rows, ["", {}]])}>
          Add model
        </Button>
        <Button
          size="sm"
          variant="primary"
          disabled={save.isPending}
          onClick={() =>
            save.mutate((w) => {
              w.prices = Object.fromEntries(rows.filter(([m]) => m.trim()).map(([m, p]) => [m.trim(), Object.fromEntries(Object.entries(p).filter(([, v]) => v !== undefined && !isNaN(v as number)))]));
              return w;
            })
          }
        >
          {save.isPending ? <Spinner /> : <Check className="size-4" />} Save prices
        </Button>
        {save.isSuccess ? <span className="self-center text-xs text-success">Saved.</span> : null}
      </div>
      <ErrorNote error={save.error} />
    </div>
  );
}

function AppPrefs() {
  const [notify, setNotify] = useLocal("devflow.notify", false);
  const { ws, save } = useWorkspacePatch();
  const [port, setPort] = useState("");
  const qc = useQueryClient();
  const refresh = useMutation({ mutationFn: api.refreshWorkflows, onSuccess: (d) => qc.setQueryData(["workflows"], d) });
  useEffect(() => {
    if (ws.data) setPort(String(ws.data.data.ui?.port ?? 8765));
  }, [ws.data]);
  const perm = typeof Notification === "undefined" ? "unsupported" : Notification.permission;
  return (
    <div className="space-y-4">
      <Card className="p-5">
        <h3 className="text-sm font-semibold">Notifications</h3>
        <p className="mt-1 text-xs text-muted-foreground">A browser notification when a run starts waiting on you or fails, while this tab is in the background.</p>
        <label className="mt-3 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={notify}
            className="accent-[var(--primary)]"
            onChange={async (e) => {
              if (e.target.checked && typeof Notification !== "undefined" && Notification.permission === "default") await Notification.requestPermission();
              setNotify(e.target.checked);
            }}
          />
          Notify me {perm === "denied" ? <span className="text-xs text-warning">(blocked in this browser's site settings)</span> : null}
        </label>
      </Card>
      <Card className="p-5">
        <h3 className="text-sm font-semibold">Theme</h3>
        <p className="mt-1 text-xs text-muted-foreground">Dark. A light theme from the same tokens is planned for later.</p>
      </Card>
      <Card className="p-5">
        <h3 className="text-sm font-semibold">Port</h3>
        <p className="mt-1 text-xs text-muted-foreground">Used the next time you run devflow ui. The server always listens on 127.0.0.1 only.</p>
        <div className="mt-3 flex max-w-xs gap-2">
          <Label htmlFor="port">
            <span className="sr-only">Port</span>
          </Label>
          <Input id="port" type="number" value={port} onChange={(e) => setPort(e.target.value)} className="font-mono" />
          <Button
            disabled={save.isPending}
            onClick={() =>
              save.mutate((w) => {
                w.ui = { ...(w.ui ?? {}), port: Number(port) };
                return w;
              })
            }
          >
            Save
          </Button>
        </div>
        <ErrorNote error={save.error} />
      </Card>
      <Card className="p-5">
        <h3 className="text-sm font-semibold">Workflows</h3>
        <p className="mt-1 text-xs text-muted-foreground">Workflows are found in langgraph.json, register_workflow calls, devflow.workflows entry points and ui.graph_sources. Re-scan after adding one.</p>
        <Button className="mt-3" size="sm" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          {refresh.isPending ? <Spinner /> : <RefreshCw className="size-3.5" />} Re-scan workflows
        </Button>
        {refresh.data ? <p className="mt-2 text-xs text-muted-foreground">{refresh.data.workflows.length} workflows found.</p> : null}
        {refresh.data?.errors.length ? (
          <ul className="mt-2 space-y-1 text-xs text-destructive">
            {refresh.data.errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        ) : null}
      </Card>
    </div>
  );
}
