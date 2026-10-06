// Small shadcn-style building blocks in the reference look: 1px borders, soft-tinted status colours, 150ms transitions.
import { Copy, X } from "lucide-react";
import { forwardRef, useEffect, useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import type { Status } from "@/lib/api";
import { cn } from "@/lib/format";

type Variant = "primary" | "outline" | "ghost" | "success" | "danger" | "danger-outline" | "warning";

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }>(
  ({ className, variant = "outline", size = "md", ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium whitespace-nowrap transition-colors duration-150 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60",
        size === "sm" ? "h-8 px-2.5 text-xs" : "h-9 px-3.5 text-sm",
        variant === "primary" && "bg-primary text-white hover:bg-primary/90",
        variant === "success" && "bg-success text-[#06210f] hover:bg-success/90",
        variant === "danger" && "bg-destructive text-white hover:bg-destructive/90",
        variant === "warning" && "bg-warning text-[#2a1d00] hover:bg-warning/90",
        variant === "outline" && "border border-border bg-transparent text-foreground hover:bg-muted",
        variant === "danger-outline" && "border border-destructive/50 text-destructive hover:bg-destructive-soft",
        variant === "ghost" && "text-muted-foreground hover:bg-muted hover:text-foreground",
        className,
      )}
      {...props}
    />
  ),
);
Button.displayName = "Button";

export function Card({ className, children, ...rest }: { className?: string; children: ReactNode } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("rounded-xl border border-border bg-card", className)} {...rest}>
      {children}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-9 w-full rounded-lg border border-input bg-surface px-3 text-sm text-foreground placeholder:text-muted-foreground/70 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-primary/60 focus:border-primary",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "min-h-20 w-full rounded-lg border border-input bg-surface px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/70 focus:outline-none focus:ring-2 focus:ring-primary/60 focus:border-primary",
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "h-9 w-full rounded-lg border border-input bg-surface px-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/60",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
}

export function Label({ children, hint, htmlFor }: { children: ReactNode; hint?: ReactNode; htmlFor?: string }) {
  return (
    <label htmlFor={htmlFor} className="mb-1.5 block text-xs font-medium text-foreground">
      {children}
      {hint ? <span className="ml-1 font-normal text-muted-foreground">{hint}</span> : null}
    </label>
  );
}

export type Tone = "primary" | "success" | "warning" | "destructive" | "muted";

const toneText: Record<Tone, string> = {
  primary: "text-primary",
  success: "text-success",
  warning: "text-warning",
  destructive: "text-destructive",
  muted: "text-muted-foreground",
};
const toneBg: Record<Tone, string> = {
  primary: "bg-primary-soft",
  success: "bg-success-soft",
  warning: "bg-warning-soft",
  destructive: "bg-destructive-soft",
  muted: "bg-muted",
};
const toneDot: Record<Tone, string> = {
  primary: "bg-primary",
  success: "bg-success",
  warning: "bg-warning",
  destructive: "bg-destructive",
  muted: "bg-muted-foreground",
};

export function Pill({ tone, children, pulse, className }: { tone: Tone; children: ReactNode; pulse?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-medium whitespace-nowrap", toneBg[tone], toneText[tone], className)}>
      <span className={cn("size-1.5 rounded-full", toneDot[tone], pulse && "animate-slow-pulse")} />
      {children}
    </span>
  );
}

export function statusTone(s: Status | string, stale?: boolean): Tone {
  if (stale) return "destructive";
  return ({ RUNNING: "primary", WAITING_HUMAN: "warning", COMPLETED: "success", FAILED: "destructive", PENDING: "muted", ABORTED: "muted" } as Record<string, Tone>)[s] ?? "muted";
}

export function statusLabel(s: Status | string, opts: { stale?: boolean; queued?: boolean } = {}): string {
  if (opts.stale) return "Stalled";
  if (s === "PENDING") return opts.queued ? "Queued" : "Pending";
  return ({ RUNNING: "Running", WAITING_HUMAN: "Needs you", COMPLETED: "Completed", FAILED: "Failed", ABORTED: "Aborted" } as Record<string, string>)[s] ?? s;
}

export function StatusPill({ status, stale, queued, className }: { status: Status; stale?: boolean; queued?: boolean; className?: string }) {
  return (
    <Pill tone={statusTone(status, stale)} pulse={status === "RUNNING" && !stale} className={className}>
      {statusLabel(status, { stale, queued })}
    </Pill>
  );
}

export function IconSquare({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return <span className={cn("inline-flex size-8 shrink-0 items-center justify-center rounded-lg", toneBg[tone], toneText[tone], className)}>{children}</span>;
}

export function Tabs<T extends string>({ tabs, value, onChange, className }: { tabs: { id: T; label: ReactNode }[]; value: T; onChange: (v: T) => void; className?: string }) {
  return (
    <div className={cn("flex gap-5 overflow-x-auto border-b border-border", className)} role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
          className={cn(
            "-mb-px whitespace-nowrap border-b-2 pb-2.5 pt-1 text-sm transition-colors duration-150",
            value === t.id ? "border-primary text-foreground font-medium" : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Segmented<T extends string>({ options, value, onChange, className }: { options: { id: T; label: ReactNode }[]; value: T; onChange: (v: T) => void; className?: string }) {
  return (
    <div className={cn("inline-flex rounded-lg border border-border p-0.5", className)}>
      {options.map((o) => (
        <button
          key={o.id}
          onClick={() => onChange(o.id)}
          aria-pressed={value === o.id}
          className={cn(
            "inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs transition-colors duration-150",
            value === o.id ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Chip({ active, children, onClick }: { active?: boolean; children: ReactNode; onClick?: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={!!active}
      className={cn(
        "rounded-md px-2.5 py-1 text-xs transition-colors duration-150",
        active ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

export function Modal({ open, onClose, title, subtitle, children, footer, width = "max-w-lg" }: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: string;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 pt-[8vh]" onMouseDown={onClose}>
      <div role="dialog" aria-modal="true" className={cn("w-full rounded-xl border border-border bg-popover shadow-2xl", width)} onMouseDown={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-4 p-5 pb-3">
          <div>
            <h2 className="text-base font-semibold">{title}</h2>
            {subtitle ? <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p> : null}
          </div>
          <button onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground" aria-label="Close">
            <X className="size-4" />
          </button>
        </div>
        <div className="max-h-[65vh] overflow-y-auto px-5 pb-2">{children}</div>
        {footer ? <div className="flex justify-end gap-2 p-5 pt-4">{footer}</div> : null}
      </div>
    </div>
  );
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      {icon ? <div className="text-muted-foreground">{icon}</div> : null}
      <div className="text-sm font-medium">{title}</div>
      {children ? <div className="max-w-sm text-xs text-muted-foreground">{children}</div> : null}
    </div>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("font-mono text-[12px]", className)}>{children}</span>;
}

export function SectionLabel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("text-[11px] font-medium uppercase tracking-widest text-muted-foreground", className)}>{children}</div>;
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return <div className="rounded-lg border border-destructive/40 bg-destructive-soft px-3 py-2 text-xs text-destructive">{msg}</div>;
}

/** A failure's whole message: clamped to a few lines until expanded, always copyable. */
export function FailureDetail({ text, title = "Why it failed" }: { text: string; title?: string }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  const long = text.length > 240 || text.split("\n").length > 3;
  return (
    <div className="rounded-lg border border-destructive/40 bg-destructive-soft px-3 py-2 text-xs text-destructive" data-testid="failure-detail">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="font-medium">{title}</span>
        <span className="flex items-center gap-3">
          {long ? (
            <button type="button" className="underline-offset-2 hover:underline" onClick={() => setOpen(!open)} data-testid="failure-toggle">
              {open ? "Collapse" : "Show full error"}
            </button>
          ) : null}
          <button
            type="button"
            className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
            onClick={() => navigator.clipboard?.writeText(text).then(() => (setCopied(true), window.setTimeout(() => setCopied(false), 1200)))}
          >
            <Copy className="size-3" /> {copied ? "Copied" : "Copy"}
          </button>
        </span>
      </div>
      <pre className={cn("whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed", open || !long ? "max-h-[420px] overflow-auto" : "line-clamp-3")}>{text}</pre>
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <span className={cn("inline-block size-3.5 animate-spin rounded-full border-2 border-current border-r-transparent", className)} />;
}
