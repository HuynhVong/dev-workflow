import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type DevEvent, type Workflow } from "./api";

export function useMeta() {
  return useQuery({ queryKey: ["meta"], queryFn: api.meta, refetchInterval: 10_000 });
}

export function useWorkflows(enabled = true) {
  return useQuery({ queryKey: ["workflows"], queryFn: api.workflows, enabled, staleTime: 60_000 });
}

export function useWorkflowMap(): Record<string, Workflow> {
  const { data } = useWorkflows();
  return useMemo(() => Object.fromEntries((data?.workflows ?? []).map((w) => [w.id, w])), [data]);
}

export function useRuns(enabled = true) {
  return useQuery({ queryKey: ["runs"], queryFn: api.runs, enabled, refetchInterval: 15_000 });
}

export function useRun(id?: string) {
  return useQuery({ queryKey: ["run", id], queryFn: () => api.run(id!), enabled: !!id, refetchInterval: 15_000 });
}

export function useRunEvents(id?: string) {
  return useQuery({ queryKey: ["events", id], queryFn: () => api.events(id!).then((r) => r.events), enabled: !!id });
}

export function useDoctor(enabled = true) {
  return useQuery({ queryKey: ["doctor"], queryFn: api.doctor, enabled, staleTime: 5 * 60_000 });
}

/** One SSE connection for the whole app: every event refreshes what it touches, so every screen is live. */
export function useLiveEvents(enabled: boolean) {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  const timers = useRef<Record<string, number>>({});

  useEffect(() => {
    if (!enabled) return;
    let es: EventSource | null = null;
    let retry = 0;
    let closed = false;

    const later = (key: string, fn: () => void, ms = 250) => {
      if (timers.current[key]) return;
      timers.current[key] = window.setTimeout(() => {
        delete timers.current[key];
        fn();
      }, ms);
    };

    const open = () => {
      es = new EventSource("/api/stream");
      es.addEventListener("hello", () => {
        retry = 0;
        setConnected(true);
      });
      es.addEventListener("devflow", (msg) => {
        const e = JSON.parse((msg as MessageEvent).data) as DevEvent;
        qc.setQueryData<DevEvent[]>(["events", e.run_id], (old) => (old && !old.some((x) => x.seq === e.seq) ? [...old, e] : old));
        later(`run:${e.run_id}`, () => qc.invalidateQueries({ queryKey: ["run", e.run_id] }));
        if (e.kind !== "agent_activity") later("runs", () => qc.invalidateQueries({ queryKey: ["runs"] }), 400);
        if (e.kind === "usage") {
          later(`usage:${e.run_id}`, () => qc.invalidateQueries({ queryKey: ["run-usage", e.run_id] }), 800);
          later("usage", () => qc.invalidateQueries({ queryKey: ["usage"] }), 2000);
        }
        if (e.kind === "status" || e.kind === "checkpoint_waiting") {
          later("meta", () => qc.invalidateQueries({ queryKey: ["meta"] }), 400);
          window.dispatchEvent(new CustomEvent("devflow:status", { detail: e }));
        }
      });
      es.onerror = () => {
        setConnected(false);
        es?.close();
        if (!closed) window.setTimeout(open, Math.min(10_000, 500 * 2 ** retry++));
      };
    };
    open();
    return () => {
      closed = true;
      es?.close();
    };
  }, [enabled, qc]);

  return connected;
}

/** Re-render every `ms` so relative times stay fresh. */
export function useNow(ms = 15_000): number {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export function useLocal<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = (nv: T) => {
    setV(nv);
    try {
      localStorage.setItem(key, JSON.stringify(nv));
    } catch {
      /* private mode */
    }
  };
  return [v, set];
}
