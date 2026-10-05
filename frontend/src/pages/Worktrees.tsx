import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { CopyButton } from "@/components/run/Approval";
import { Button, Card, Empty, ErrorNote, Modal, Mono, Pill, Spinner, StatusPill } from "@/components/ui";
import { api, type Worktree } from "@/lib/api";
import { bytes } from "@/lib/format";

export function Worktrees() {
  const q = useQuery({ queryKey: ["worktrees"], queryFn: api.worktrees, refetchInterval: 20_000 });
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<Worktree | null>(null);
  const clean = useMutation({
    mutationFn: (w: Worktree) => api.cleanWorktree(w.ticket, w.repo),
    onSuccess: () => {
      setConfirm(null);
      qc.invalidateQueries({ queryKey: ["worktrees"] });
    },
  });
  const rows = q.data?.worktrees ?? [];
  return (
    <div className="mx-auto max-w-[1400px]">
      <h1 className="text-[28px] font-bold tracking-tight">Worktrees</h1>
      <p className="mb-6 mt-1 text-sm text-muted-foreground">
        One per ticket and repo under <Mono>{q.data?.root ?? "…"}</Mono>. Your own clones are never edited. Clean-up is never forced and always keeps the branch.
      </p>
      <Card className="overflow-x-auto">
        {q.isLoading ? (
          <div className="p-6">
            <Spinner className="text-muted-foreground" />
          </div>
        ) : null}
        {!q.isLoading && !rows.length ? <Empty title="No worktrees yet">Each ticket run creates its own.</Empty> : null}
        {rows.length ? (
          <table className="w-full text-sm" data-testid="worktrees">
            <thead className="text-xs text-muted-foreground">
              <tr className="border-b border-border">
                {["Ticket", "Repo", "Branch", "Changes", "Size", "Run", "Path", ""].map((h) => (
                  <th key={h} className="px-4 py-2.5 text-left font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((w) => (
                <tr key={w.path} className="border-b border-border/60 last:border-0">
                  <td className="px-4 py-2.5 font-mono text-[12px]">{w.ticket}</td>
                  <td className="px-4 py-2.5 font-mono text-[12px]">{w.repo}</td>
                  <td className="px-4 py-2.5 font-mono text-[12px]">{w.branch}</td>
                  <td className="px-4 py-2.5">{w.dirty === null ? <span className="text-xs text-muted-foreground">unknown</span> : w.dirty ? <Pill tone="warning">Uncommitted</Pill> : <Pill tone="success">Clean</Pill>}</td>
                  <td className="px-4 py-2.5 text-xs text-muted-foreground">{bytes(w.size_bytes)}</td>
                  <td className="px-4 py-2.5">
                    {w.run ? (
                      <Link to={`/?run=${encodeURIComponent(w.run.run_id)}`} className="inline-flex">
                        <StatusPill status={w.run.status} stale={w.run.stale} />
                      </Link>
                    ) : (
                      <span className="text-xs text-muted-foreground">none</span>
                    )}
                  </td>
                  <td className="max-w-[280px] px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <Mono className="truncate text-muted-foreground">{w.path}</Mono>
                      <CopyButton text={w.path} label="" />
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!w.can_clean}
                      title={w.can_clean ? "Remove this worktree (the branch is kept)" : w.dirty ? "It has uncommitted changes" : "Its run is not finished"}
                      onClick={() => setConfirm(w)}
                    >
                      <Trash2 className="size-3.5" /> Clean up
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Card>
      <ErrorNote error={q.error} />
      <Modal
        open={!!confirm}
        onClose={() => setConfirm(null)}
        title="Clean up this worktree?"
        subtitle="It is removed with git worktree remove (never forced). The branch and its commits stay in your clone."
        footer={
          <>
            <Button onClick={() => setConfirm(null)}>Cancel</Button>
            <Button variant="danger" disabled={clean.isPending} onClick={() => confirm && clean.mutate(confirm)}>
              {clean.isPending ? <Spinner /> : <Trash2 className="size-4" />} Clean up
            </Button>
          </>
        }
      >
        {confirm ? (
          <p className="text-sm">
            <Mono>{confirm.path}</Mono>
          </p>
        ) : null}
        <div className="mt-2">
          <ErrorNote error={clean.error} />
        </div>
      </Modal>
    </div>
  );
}
