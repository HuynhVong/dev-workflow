import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function ago(ts?: number | null, now = Date.now() / 1000): string {
  if (!ts) return "";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function duration(s?: number | null): string {
  if (s == null || !isFinite(s)) return "";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export function tokens(n?: number | null): string {
  n = n || 0;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k`;
  return String(n);
}

export function usd(n?: number | null): string {
  if (n == null) return "";
  if (n > 0 && n < 0.01) return "<$0.01";
  return `$${n.toFixed(2)}`;
}

export function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dateTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function bytes(n?: number | null): string {
  if (n == null) return "";
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(0)} MB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(0)} KB`;
  return `${n} B`;
}

export function initials(name: string): string {
  const parts = name.replace(/[._-]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "DV";
  return (parts[0][0] + (parts[1]?.[0] ?? parts[0][1] ?? "")).toUpperCase();
}

/** "AQS-5512: Fix rounding" -> title part; falls back to the ticket or run id. */
export function runTitle(r: { label?: string; ticket?: string; run_id: string; ticket_info?: { title: string } }): string {
  if (r.ticket_info?.title) return r.ticket_info.title;
  if (r.label) {
    const i = r.label.indexOf(": ");
    return i > 0 && r.ticket && r.label.startsWith(r.ticket) ? r.label.slice(i + 2) : r.label;
  }
  return r.ticket || r.run_id;
}

export function humanize(id: string): string {
  return id.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
