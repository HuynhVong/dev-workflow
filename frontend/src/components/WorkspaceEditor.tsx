import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, FolderGit2, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { dumpYaml } from "@/lib/yaml";
import { Button, Card, ErrorNote, Input, Label, Spinner, Textarea } from "./ui";

type Repo = {
  path: string;
  gitlab_project?: string;
  base_branch?: string;
  has_ui?: boolean;
  commands?: Record<string, string>;
  setup_commands?: string[];
  copy_files?: string[];
  parallel_checks?: boolean;
};

type Data = Record<string, any> & { repos?: Record<string, Repo> };

const STARTER: Data = {
  repos: {},
  mcp_servers: {
    atlassian: { command: "uvx", args: ["mcp-atlassian"], env: { JIRA_URL: "", JIRA_USERNAME: "", JIRA_API_TOKEN: "${JIRA_API_TOKEN}", CONFLUENCE_URL: "", CONFLUENCE_USERNAME: "", CONFLUENCE_API_TOKEN: "${CONFLUENCE_API_TOKEN}" } },
    playwright: { command: "npx", args: ["@playwright/mcp@latest"] },
  },
  jira_server: "atlassian",
  confluence_server: "atlassian",
  jira_user: "",
  state_dir: ".devflow",
  worktree_root: "~/devflow-worktrees",
  max_parallel_runs: 3,
};

/** A form over workspace.yaml. Secrets come back masked and are kept as they are unless you type a new value. */
export function WorkspaceEditor({ onSaved, saveLabel = "Save workspace" }: { onSaved?: () => void; saveLabel?: string }) {
  const q = useQuery({ queryKey: ["workspace"], queryFn: api.workspace });
  const qc = useQueryClient();
  const [data, setData] = useState<Data | null>(null);
  const [mcpText, setMcpText] = useState("");
  const [mcpError, setMcpError] = useState("");
  const [showYaml, setShowYaml] = useState(false);

  useEffect(() => {
    if (q.data && data === null) {
      const d = q.data.exists && Object.keys(q.data.data).length ? q.data.data : STARTER;
      setData(structuredClone(d));
      setMcpText(JSON.stringify(d.mcp_servers ?? {}, null, 2));
    }
  }, [q.data, data]);

  const save = useMutation({
    mutationFn: (d: Data) => api.saveWorkspace(d),
    onSuccess: (r) => {
      setData(structuredClone(r.data));
      qc.invalidateQueries();
      onSaved?.();
    },
  });

  if (!data) return <Spinner className="text-muted-foreground" />;
  const repos = data.repos ?? {};
  const set = (patch: Partial<Data>) => setData({ ...data, ...patch });
  const setRepo = (name: string, patch: Partial<Repo>) => set({ repos: { ...repos, [name]: { ...repos[name], ...patch } } });
  const renameRepo = (old: string, name: string) => {
    const next: Record<string, Repo> = {};
    for (const [k, v] of Object.entries(repos)) next[k === old ? name : k] = v;
    set({ repos: next });
  };
  const addRepo = () => {
    let n = "new-repo";
    for (let i = 2; repos[n]; i++) n = `new-repo-${i}`;
    set({ repos: { ...repos, [n]: { path: "", base_branch: "develop", has_ui: false, commands: {} } } });
  };
  const removeRepo = (name: string) => {
    const next = { ...repos };
    delete next[name];
    set({ repos: next });
  };
  const final = (): Data | null => {
    try {
      const mcp = JSON.parse(mcpText || "{}");
      return { ...data, mcp_servers: mcp };
    } catch {
      return null;
    }
  };

  return (
    <div className="space-y-5" data-testid="workspace-editor">
      <p className="text-xs text-muted-foreground">
        File: <span className="font-mono">{q.data?.path}</span>
        {q.data?.exists ? ". Saving rewrites it, so comments in it are not kept." : " (it will be created)"}
      </p>
      <Card className="p-4 sm:p-5">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Repos</h3>
          <Button size="sm" onClick={addRepo} data-testid="add-repo">
            <Plus className="size-3.5" /> Add repo
          </Button>
        </div>
        <div className="space-y-3">
          {Object.entries(repos).map(([name, r], i) => (
            <RepoEditor key={i} name={name} repo={r} onRename={(n) => renameRepo(name, n)} onChange={(p) => setRepo(name, p)} onRemove={() => removeRepo(name)} />
          ))}
          {!Object.keys(repos).length ? <p className="text-xs text-muted-foreground">Add each git repo devflow may work in. Your clone is only read; runs work in their own worktrees.</p> : null}
        </div>
      </Card>
      <Card className="grid gap-4 p-4 sm:grid-cols-2 sm:p-5">
        <TextField label="Jira user" hint="(your Jira email)" value={data.jira_user} onChange={(v) => set({ jira_user: v })} />
        <TextField label="State folder" hint="(SQLite history)" value={data.state_dir} onChange={(v) => set({ state_dir: v })} mono />
        <TextField label="Worktree root" value={data.worktree_root} onChange={(v) => set({ worktree_root: v })} mono />
        <div>
          <Label htmlFor="ws-max">Runs at once</Label>
          <Input id="ws-max" type="number" min={1} max={10} value={data.max_parallel_runs ?? 3} onChange={(e) => set({ max_parallel_runs: Number(e.target.value) })} />
        </div>
        <TextField label="Jira MCP server" value={data.jira_server} onChange={(v) => set({ jira_server: v })} mono />
        <TextField label="Confluence MCP server" hint="(read only)" value={data.confluence_server} onChange={(v) => set({ confluence_server: v })} mono />
      </Card>
      <Card className="p-4 sm:p-5">
        <h3 className="mb-1 text-sm font-semibold">MCP servers</h3>
        <p className="mb-2 text-xs text-muted-foreground">
          JSON. Values shown as {q.data?.mask ?? "••••"} are secrets kept from the file; leave them to keep them. Prefer <span className="font-mono">${"{ENV_VAR}"}</span> references for tokens.
        </p>
        <Textarea
          rows={10}
          className="font-mono text-xs"
          value={mcpText}
          onChange={(e) => {
            setMcpText(e.target.value);
            try {
              JSON.parse(e.target.value || "{}");
              setMcpError("");
            } catch (err) {
              setMcpError((err as Error).message);
            }
          }}
          aria-label="MCP servers"
        />
        {mcpError ? <p className="mt-1 text-xs text-destructive">{mcpError}</p> : null}
        {q.data?.env_refs && Object.keys(q.data.env_refs).length ? (
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            {Object.entries(q.data.env_refs).map(([k, v]) => (
              <span key={k} className={v === "set" ? "text-success" : "text-warning"}>
                ${k} {v}
              </span>
            ))}
          </div>
        ) : null}
      </Card>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" disabled={save.isPending || !!mcpError} onClick={() => final() && save.mutate(final()!)} data-testid="save-workspace">
          {save.isPending ? <Spinner /> : <Check className="size-4" />} {saveLabel}
        </Button>
        <Button variant="ghost" onClick={() => setShowYaml(!showYaml)}>
          {showYaml ? "Hide" : "Preview"} YAML
        </Button>
        {save.isSuccess ? <span className="text-xs text-success">Saved.</span> : null}
      </div>
      <ErrorNote error={save.error} />
      {showYaml ? <pre className="max-h-96 overflow-auto rounded-lg border border-border bg-surface p-3 font-mono text-xs">{dumpYaml(final() ?? data)}</pre> : null}
    </div>
  );
}

function TextField({ label, hint, value, onChange, mono }: { label: string; hint?: string; value?: string; onChange: (v: string) => void; mono?: boolean }) {
  const id = `ws-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div>
      <Label htmlFor={id} hint={hint}>
        {label}
      </Label>
      <Input id={id} value={value ?? ""} onChange={(e) => onChange(e.target.value)} className={mono ? "font-mono text-xs" : ""} />
    </div>
  );
}

function RepoEditor({ name, repo, onRename, onChange, onRemove }: { name: string; repo: Repo; onRename: (n: string) => void; onChange: (p: Partial<Repo>) => void; onRemove: () => void }) {
  const [cmds, setCmds] = useState(Object.entries(repo.commands ?? {}).map(([k, v]) => `${k}: ${v}`).join("\n"));
  return (
    <div className="rounded-lg border border-border bg-surface p-3" data-testid={`repo-editor-${name}`}>
      <div className="grid gap-3 sm:grid-cols-[1fr_2fr_auto]">
        <div>
          <Label>Name</Label>
          <Input value={name} onChange={(e) => onRename(e.target.value.replace(/\s+/g, "-"))} className="font-mono text-xs" aria-label="Repo name" />
        </div>
        <div>
          <Label hint="(your local clone)">Path</Label>
          <div className="relative">
            <FolderGit2 className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input value={repo.path} onChange={(e) => onChange({ path: e.target.value })} placeholder="~/code/api-service" className="pl-9 font-mono text-xs" aria-label="Repo path" />
          </div>
        </div>
        <div className="flex items-end">
          <Button variant="ghost" size="sm" onClick={onRemove} aria-label={`Remove ${name}`}>
            <Trash2 className="size-4" />
          </Button>
        </div>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <div>
          <Label>Base branch</Label>
          <Input value={repo.base_branch ?? "develop"} onChange={(e) => onChange({ base_branch: e.target.value })} className="font-mono text-xs" />
        </div>
        <div>
          <Label>GitLab project</Label>
          <Input value={repo.gitlab_project ?? ""} onChange={(e) => onChange({ gitlab_project: e.target.value })} placeholder="group/project" className="font-mono text-xs" />
        </div>
        <div className="flex items-end gap-4 pb-2 text-xs">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={!!repo.has_ui} onChange={(e) => onChange({ has_ui: e.target.checked })} className="accent-[var(--primary)]" /> Has a UI
          </label>
          <label className="flex items-center gap-1.5" title="Off when its checks use a fixed port or shared database">
            <input type="checkbox" checked={repo.parallel_checks !== false} onChange={(e) => onChange({ parallel_checks: e.target.checked })} className="accent-[var(--primary)]" /> Parallel checks
          </label>
        </div>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <div className="sm:col-span-1">
          <Label hint="(name: command)">Commands</Label>
          <Textarea
            rows={3}
            className="font-mono text-xs"
            value={cmds}
            placeholder={"lint: npm run lint\ntest: npm test\nrun: npm run dev"}
            onChange={(e) => {
              setCmds(e.target.value);
              const out: Record<string, string> = {};
              for (const line of e.target.value.split("\n")) {
                const i = line.indexOf(":");
                if (i > 0 && line.slice(i + 1).trim()) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
              }
              onChange({ commands: out });
            }}
          />
        </div>
        <div>
          <Label hint="(one per line)">Worktree setup</Label>
          <Textarea rows={3} className="font-mono text-xs" value={(repo.setup_commands ?? []).join("\n")} placeholder="npm ci" onChange={(e) => onChange({ setup_commands: lines(e.target.value) })} />
        </div>
        <div>
          <Label hint="(one per line)">Files to copy</Label>
          <Textarea rows={3} className="font-mono text-xs" value={(repo.copy_files ?? []).join("\n")} placeholder=".env.local" onChange={(e) => onChange({ copy_files: lines(e.target.value) })} />
        </div>
      </div>
    </div>
  );
}

function lines(s: string): string[] {
  return s.split("\n").map((x) => x.trim()).filter(Boolean);
}
