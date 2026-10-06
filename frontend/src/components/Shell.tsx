import { Bell, Coins, FolderGit2, GitBranch, LayoutGrid, Menu, MoreHorizontal, Settings, ShieldCheck, Workflow as WorkflowIcon, X } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import type { DevEvent } from "@/lib/api";
import { ago, cn, initials, runTitle } from "@/lib/format";
import { useMeta, useRuns, useWorkflows } from "@/lib/hooks";
import { SectionLabel } from "./ui";

export function Shell({ children, crumbs, onNewRun }: { children: ReactNode; crumbs: string[]; onNewRun: (workflow?: string) => void }) {
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  useEffect(() => setOpen(false), [loc.pathname, loc.search]);
  return (
    // The shell is exactly one viewport tall and only the content column scrolls, so the sidebar stays put and
    // its background always reaches the bottom of the window, however long the page is.
    <div className="flex h-screen overflow-hidden">
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 w-[228px] shrink-0 border-r border-border bg-sidebar transition-transform duration-150 lg:static lg:h-full lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <Sidebar onNewRun={onNewRun} onClose={() => setOpen(false)} />
      </aside>
      {open ? <div className="fixed inset-0 z-30 bg-black/50 lg:hidden" onClick={() => setOpen(false)} /> : null}
      <div className="flex min-w-0 flex-1 flex-col overflow-y-auto" data-testid="content-scroll">
        <Topbar crumbs={crumbs} onMenu={() => setOpen(true)} />
        <main className="min-w-0 flex-1 px-4 pb-10 pt-6 sm:px-6 lg:px-8">{children}</main>
      </div>
    </div>
  );
}

function Sidebar({ onNewRun, onClose }: { onNewRun: (workflow?: string) => void; onClose: () => void }) {
  const runs = useRuns().data?.runs ?? [];
  const wfs = useWorkflows().data?.workflows ?? [];
  const meta = useMeta().data;
  const [params] = useSearchParams();
  const loc = useLocation();
  const needs = runs.filter((r) => r.status === "WAITING_HUMAN").length;
  const activeWf = loc.pathname === "/runs" || loc.pathname === "/" ? params.get("workflow") : null;
  const perWf = useMemo(() => {
    const m: Record<string, number> = {};
    for (const r of runs) if (["RUNNING", "WAITING_HUMAN", "PENDING", "FAILED"].includes(r.status)) m[r.workflow] = (m[r.workflow] ?? 0) + 1;
    return m;
  }, [runs]);

  const isActive = (to: string) => {
    const filter = params.get("filter");
    if (to === "/") return loc.pathname === "/";
    if (to === "/runs") return loc.pathname === "/runs" && filter !== "needs" && !params.get("workflow");
    if (to === "/runs?filter=needs") return loc.pathname === "/runs" && filter === "needs";
    return loc.pathname.startsWith(to);
  };
  const item = (to: string, icon: ReactNode, label: string, count?: ReactNode) => (
    <NavLink
      to={to}
      className={cn(
        "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors duration-150",
        isActive(to) ? "bg-raised text-foreground" : "text-muted-foreground hover:bg-raised/60 hover:text-foreground",
      )}
    >
      <span className="[&>svg]:size-4">{icon}</span>
      <span className="flex-1">{label}</span>
      {count}
    </NavLink>
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[72px] items-center justify-between px-5">
        <Link to="/" className="flex items-center gap-2.5">
          <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-white">
            <WorkflowIcon className="size-4" />
          </span>
          <span className="text-[17px] font-bold tracking-tight">
            devflow<span className="text-primary">.</span>
          </span>
        </Link>
        <button className="rounded-md p-1 text-muted-foreground lg:hidden" onClick={onClose} aria-label="Close menu">
          <X className="size-4" />
        </button>
      </div>
      <nav className="flex-1 space-y-6 overflow-y-auto px-3 pb-4">
        <div>
          <SectionLabel className="mb-2 px-2.5">Workspace</SectionLabel>
          <div className="space-y-0.5">
            {item("/", <LayoutGrid />, "Overview")}
            {item("/runs", <GitBranch />, "All runs", <span className="text-xs text-muted-foreground">{runs.length}</span>)}
            {item(
              "/runs?filter=needs",
              <ShieldCheck />,
              "Approvals",
              needs ? <span className="rounded-md bg-warning px-1.5 text-[11px] font-semibold text-[#2a1d00]">{needs}</span> : null,
            )}
            {item("/tokens", <Coins />, "Tokens")}
            {item("/worktrees", <FolderGit2 />, "Worktrees")}
            {item("/settings", <Settings />, "Settings")}
          </div>
        </div>
        <div>
          <SectionLabel className="mb-2 px-2.5">Your workflows</SectionLabel>
          <div className="space-y-0.5">
            {wfs.map((w) => (
              <div key={w.id} className="group flex items-center">
                <Link
                  to={`/runs?workflow=${encodeURIComponent(w.id)}`}
                  title={w.description}
                  className={cn(
                    "flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors duration-150",
                    activeWf === w.id ? "bg-raised text-foreground" : "text-muted-foreground hover:bg-raised/60 hover:text-foreground",
                  )}
                >
                  <span className="size-2 shrink-0 rounded-full" style={{ background: w.color }} />
                  <span className="truncate">{w.title}</span>
                  {perWf[w.id] ? <span className="ml-auto text-xs">{perWf[w.id]}</span> : null}
                </Link>
                <button
                  className="ml-0.5 hidden rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-raised hover:text-foreground group-hover:block"
                  title={`Start ${w.title}`}
                  aria-label={`Start ${w.title}`}
                  onClick={() => onNewRun(w.id)}
                >
                  +
                </button>
              </div>
            ))}
            {!wfs.length ? <div className="px-2.5 text-xs text-muted-foreground">None found yet.</div> : null}
          </div>
        </div>
      </nav>
      <div className="flex items-center gap-2.5 border-t border-border px-4 py-3.5">
        <Avatar name={meta?.user || "devflow"} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{meta?.user || "You"}</div>
          <div className="truncate text-xs text-muted-foreground">Local workspace</div>
        </div>
        <Link to="/settings" className="rounded-md p-1 text-muted-foreground hover:bg-raised hover:text-foreground" aria-label="Settings">
          <MoreHorizontal className="size-4" />
        </Link>
      </div>
    </div>
  );
}

export function Avatar({ name, className }: { name: string; className?: string }) {
  return (
    <span className={cn("inline-flex size-8 shrink-0 items-center justify-center rounded-full bg-primary-soft text-xs font-semibold text-primary", className)}>
      {initials(name)}
    </span>
  );
}

function Topbar({ crumbs, onMenu }: { crumbs: string[]; onMenu: () => void }) {
  const meta = useMeta().data;
  const runs = useRuns().data?.runs ?? [];
  const nav = useNavigate();
  const [bell, setBell] = useState(false);
  const waiting = runs.filter((r) => r.status === "WAITING_HUMAN" || (r.status === "FAILED" && !r.queued));
  const d = meta?.doctor;
  const tone = !meta?.loaded ? "destructive" : !d ? "muted" : d.status === "fail" ? "destructive" : d.status === "warn" ? "warning" : "success";
  const label = !meta?.loaded
    ? "Setup needed"
    : !d
      ? "Connections not checked"
      : d.status === "ok"
        ? "All connections OK"
        : d.status === "warn"
          ? `${d.warn} warning${d.warn === 1 ? "" : "s"}`
          : `${d.blocking} blocking problem${d.blocking === 1 ? "" : "s"}`;

  return (
    <header className="sticky top-0 z-20 flex h-[72px] items-center gap-3 border-b border-border bg-background/95 px-4 backdrop-blur sm:px-6 lg:px-8">
      <button className="rounded-md p-1.5 text-muted-foreground hover:bg-muted lg:hidden" onClick={onMenu} aria-label="Open menu">
        <Menu className="size-5" />
      </button>
      <div className="flex min-w-0 items-center gap-2 text-sm">
        {crumbs.map((c, i) => (
          <span key={i} className={cn("truncate", i === crumbs.length - 1 ? "font-semibold text-foreground" : "hidden text-muted-foreground sm:inline")}>
            {c}
            {i < crumbs.length - 1 ? <span className="ml-2 text-muted-foreground">›</span> : null}
          </span>
        ))}
      </div>
      <div className="ml-auto flex items-center gap-2">
        <Link
          to={meta?.loaded ? "/settings/connections" : "/setup"}
          className="hidden items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground sm:flex"
          data-testid="doctor-status"
        >
          <span
            className={cn(
              "size-2 rounded-full",
              tone === "success" && "bg-success",
              tone === "warning" && "bg-warning",
              tone === "destructive" && "bg-destructive",
              tone === "muted" && "bg-muted-foreground",
            )}
          />
          {label}
        </Link>
        <div className="relative">
          <button className="relative rounded-lg p-2 text-muted-foreground hover:bg-muted hover:text-foreground" onClick={() => setBell(!bell)} aria-label="Notifications">
            <Bell className="size-4" />
            {waiting.length ? <span className="absolute right-1.5 top-1.5 size-2 rounded-full bg-warning" /> : null}
          </button>
          {bell ? (
            <>
              <div className="fixed inset-0 z-30" onClick={() => setBell(false)} />
              <div className="absolute right-0 z-40 mt-2 w-80 rounded-xl border border-border bg-popover p-2 shadow-2xl">
                <div className="px-2 py-1.5 text-xs font-medium text-muted-foreground">Waiting on you</div>
                {waiting.length === 0 ? <div className="px-2 py-3 text-sm text-muted-foreground">Nothing needs you right now.</div> : null}
                {waiting.map((r) => (
                  <button
                    key={r.run_id}
                    onClick={() => {
                      setBell(false);
                      nav(`/?run=${encodeURIComponent(r.run_id)}`);
                    }}
                    className="flex w-full flex-col rounded-lg px-2 py-2 text-left hover:bg-muted"
                  >
                    <span className="text-sm">
                      {r.status === "FAILED" ? "Failed: " : ""}
                      {r.ticket || runTitle(r)}
                      {r.pending ? ` · ${r.checkpoint || r.pending.name}` : ""}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {runTitle(r)} · {ago(r.updated_at)}
                    </span>
                  </button>
                ))}
              </div>
            </>
          ) : null}
        </div>
        <Avatar name={meta?.user || "devflow"} />
      </div>
    </header>
  );
}

/** Tab title and an optional browser notification when a run starts waiting on you or fails. */
export function useAttentionSignals() {
  const runs = useRuns().data?.runs ?? [];
  const n = runs.filter((r) => r.status === "WAITING_HUMAN").length;
  useEffect(() => {
    document.title = n ? `(${n}) devflow` : "devflow";
  }, [n]);
  useEffect(() => {
    const onStatus = (ev: Event) => {
      const e = (ev as CustomEvent<DevEvent>).detail;
      const status = e.data?.status;
      const waiting = e.kind === "checkpoint_waiting" || status === "WAITING_HUMAN";
      if (!(waiting || status === "FAILED")) return;
      let enabled = false;
      try {
        enabled = localStorage.getItem("devflow.notify") === "true";
      } catch {
        /* ignore */
      }
      if (!enabled || typeof Notification === "undefined" || Notification.permission !== "granted" || document.hasFocus()) return;
      new Notification(waiting ? `${e.run_id} needs you` : `${e.run_id} failed`, { body: e.node || e.data?.detail || "", tag: e.run_id });
    };
    window.addEventListener("devflow:status", onStatus);
    return () => window.removeEventListener("devflow:status", onStatus);
  }, []);
}
