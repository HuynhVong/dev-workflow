import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Card, Empty, Segmented } from "@/components/ui";
import { Mini, TokenBars } from "@/components/run/RunTabs";
import { api } from "@/lib/api";
import { tokens, usd } from "@/lib/format";
import { useWorkflowMap } from "@/lib/hooks";

type Range = "1" | "7" | "30" | "all";

export function Tokens() {
  const [range, setRange] = useState<Range>("7");
  const since = useMemo(() => (range === "all" ? 0 : Date.now() / 1000 - Number(range) * 86400), [range]);
  const q = useQuery({ queryKey: ["usage", range], queryFn: () => api.usage(since), refetchInterval: 30_000 });
  const wfs = useWorkflowMap();
  const d = q.data;
  const t = d?.totals;
  const bar = (rows: typeof d extends undefined ? never : NonNullable<typeof d>["by_day"], name: (k: string) => string = (k) => k) =>
    rows.map((g) => ({ name: name(g.key) || "(none)", input: g.input_tokens, output: g.output_tokens, cache_read: g.cache_read_tokens, cache_write: g.cache_write_tokens }));

  return (
    <div className="mx-auto max-w-[1400px]">
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[28px] font-bold tracking-tight">Tokens and cost</h1>
          <p className="mt-1 text-sm text-muted-foreground">Every Claude call across all runs. Cost is an estimate from your price table.</p>
        </div>
        <Segmented
          value={range}
          onChange={setRange}
          options={[
            { id: "1", label: "24 hours" },
            { id: "7", label: "7 days" },
            { id: "30", label: "30 days" },
            { id: "all", label: "All" },
          ]}
        />
      </div>
      {t ? (
        <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="usage-stats">
          <Mini label="Input" value={tokens(t.input_tokens)} sub={`${t.calls} calls`} />
          <Mini label="Output" value={tokens(t.output_tokens)} />
          <Mini label="Cache hit rate" value={`${Math.round(t.cache_hit_rate * 100)}%`} sub={`${tokens(t.cache_read_tokens)} read · ${tokens(t.cache_write_tokens)} written`} />
          <Mini label="Estimated cost" value={usd(t.cost_usd) || "$0.00"} sub={t.unpriced_calls ? `${t.unpriced_calls} calls have no price set` : "All calls priced"} />
        </div>
      ) : null}
      {d && !d.totals.calls ? (
        <Card>
          <Empty title="No Claude calls in this range" />
        </Card>
      ) : null}
      {d && d.totals.calls ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card className="p-5">
            <h2 className="mb-3 text-sm font-semibold">Per day</h2>
            <TokenBars data={bar(d.by_day)} />
          </Card>
          <Card className="p-5">
            <h2 className="mb-3 text-sm font-semibold">Per workflow</h2>
            <TokenBars data={bar(d.by_workflow, (k) => wfs[k]?.title ?? k)} />
          </Card>
          <Card className="p-5">
            <h2 className="mb-3 text-sm font-semibold">Per model</h2>
            <TokenBars data={bar(d.by_model)} />
          </Card>
          <Card className="overflow-x-auto p-5">
            <h2 className="mb-3 text-sm font-semibold">Most expensive steps</h2>
            <table className="w-full text-xs">
              <thead className="text-muted-foreground">
                <tr>
                  {["Step", "Calls", "In", "Out", "Cache hit", "Cost"].map((h) => (
                    <th key={h} className="py-1.5 pr-3 text-left font-medium">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {d.by_step.map((g) => (
                  <tr key={g.key} className="border-t border-border">
                    <td className="py-1.5 pr-3 font-mono">{g.key || "(none)"}</td>
                    <td className="py-1.5 pr-3">{g.calls}</td>
                    <td className="py-1.5 pr-3 font-mono">{tokens(g.input_tokens)}</td>
                    <td className="py-1.5 pr-3 font-mono">{tokens(g.output_tokens)}</td>
                    <td className="py-1.5 pr-3">{Math.round(g.cache_hit_rate * 100)}%</td>
                    <td className="py-1.5 pr-3 font-mono">{usd(g.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      ) : null}
      {d && !d.priced_models.length ? <p className="mt-4 text-xs text-muted-foreground">No prices set, so costs show $0. Add them in Settings, Prices.</p> : null}
    </div>
  );
}
