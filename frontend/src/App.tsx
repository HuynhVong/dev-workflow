import { useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { RunDetailCard } from "@/components/run/RunDetail";
import { Shell, useAttentionSignals } from "@/components/Shell";
import { StartRunModal } from "@/components/StartRunModal";
import { Spinner } from "@/components/ui";
import { useLiveEvents, useMeta } from "@/lib/hooks";
import { Overview } from "@/pages/Overview";
import { Settings } from "@/pages/Settings";
import { Setup } from "@/pages/Setup";
import { Tokens } from "@/pages/Tokens";
import { Worktrees } from "@/pages/Worktrees";

function crumbsFor(path: string, search: string): string[] {
  if (path.startsWith("/runs/")) return ["Runs", decodeURIComponent(path.slice(6))];
  if (path === "/runs") return ["Workspace", new URLSearchParams(search).get("filter") === "needs" ? "Approvals" : "All runs"];
  if (path.startsWith("/settings")) return ["Workspace", "Settings"];
  if (path === "/tokens") return ["Workspace", "Tokens"];
  if (path === "/worktrees") return ["Workspace", "Worktrees"];
  if (path === "/setup") return ["Workspace", "Setup"];
  return ["Workspace", "Overview"];
}

export function App() {
  const meta = useMeta();
  const loaded = !!meta.data?.loaded;
  useLiveEvents(loaded);
  useAttentionSignals();
  const loc = useLocation();
  const nav = useNavigate();
  const [modal, setModal] = useState<{ open: boolean; workflow?: string }>({ open: false });
  const openNew = (workflow?: string) => setModal({ open: true, workflow });

  if (meta.isLoading) {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        <Spinner />
      </div>
    );
  }
  if (meta.error) {
    return (
      <div className="flex h-screen items-center justify-center p-6 text-center text-sm text-muted-foreground">
        Can't reach the devflow server. Open the link `devflow ui` printed in your terminal.
      </div>
    );
  }
  if (!loaded && loc.pathname !== "/setup") return <Navigate to="/setup" replace />;

  return (
    <Shell crumbs={crumbsFor(loc.pathname, loc.search)} onNewRun={openNew}>
      <Routes>
        <Route path="/" element={<Overview onNewRun={openNew} />} />
        <Route path="/runs" element={<Overview onNewRun={openNew} />} />
        <Route path="/runs/:id" element={<RunPage />} />
        <Route path="/tokens" element={<Tokens />} />
        <Route path="/worktrees" element={<Worktrees />} />
        <Route path="/settings" element={<Navigate to="/settings/workspace" replace />} />
        <Route path="/settings/:section" element={<Settings />} />
        <Route path="/setup" element={<Setup onStartRun={() => (nav("/"), openNew())} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <StartRunModal
        open={modal.open}
        initialWorkflow={modal.workflow}
        onClose={() => setModal({ open: false })}
        onStarted={(id) => {
          setModal({ open: false });
          nav(`/?run=${encodeURIComponent(id)}`);
        }}
      />
    </Shell>
  );
}

function RunPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  return (
    <div className="mx-auto max-w-[1100px]">
      <RunDetailCard runId={id} onBack={() => nav("/")} />
    </div>
  );
}
