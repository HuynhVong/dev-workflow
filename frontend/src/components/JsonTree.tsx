import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/format";

/** A collapsible JSON view: the fallback renderer for any output or payload shape. */
export function JsonTree({ value, name, depth = 0, open = 1 }: { value: unknown; name?: string; depth?: number; open?: number }) {
  const [expanded, setExpanded] = useState(depth < open);
  const isObj = value !== null && typeof value === "object";
  const key = name !== undefined ? <span className="text-muted-foreground">{name}: </span> : null;

  if (!isObj) {
    return (
      <div className="whitespace-pre-wrap break-words py-px font-mono text-[12px]" style={{ paddingLeft: depth ? 14 : 0 }}>
        {key}
        <Scalar value={value} />
      </div>
    );
  }
  const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v] as const) : Object.entries(value as Record<string, unknown>);
  const summary = Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`;
  return (
    <div className="font-mono text-[12px]" style={{ paddingLeft: depth ? 14 : 0 }}>
      <button className="flex items-center gap-0.5 py-px text-left hover:text-foreground" onClick={() => setExpanded(!expanded)}>
        <ChevronRight className={cn("size-3 shrink-0 text-muted-foreground transition-transform duration-150", expanded && "rotate-90")} />
        {key}
        <span className="text-muted-foreground">{summary}</span>
      </button>
      {expanded ? (
        <div className="border-l border-border/70 ml-1.5">
          {entries.length === 0 ? <div className="pl-3.5 text-muted-foreground">empty</div> : null}
          {entries.map(([k, v]) => (
            <JsonTree key={k} name={k} value={v} depth={depth + 1} open={open} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Scalar({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground">null</span>;
  if (typeof value === "string") return <span className="text-success/90">"{value}"</span>;
  if (typeof value === "number") return <span className="text-primary">{value}</span>;
  if (typeof value === "boolean") return <span className="text-warning">{String(value)}</span>;
  return <span>{String(value)}</span>;
}
