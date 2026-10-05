import dagre from "@dagrejs/dagre";
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { useMemo } from "react";
import type { Workflow } from "@/lib/api";
import { cn } from "@/lib/format";
import { baseId, nodeTitle, type Exec, type Step, type StepState } from "@/lib/steps";
import { Marker } from "./Stepper";

type NodeData = { title: string; state: StepState; count: number; n: number | string; selected: boolean };

function FlowNode({ data }: NodeProps<Node<NodeData>>) {
  return (
    <div
      className={cn(
        "flex w-[200px] items-center gap-2 rounded-lg border bg-card px-2 py-1.5",
        data.selected ? "border-primary" : data.state === "waiting" ? "border-warning/60" : data.state === "failed" ? "border-destructive/60" : "border-border",
      )}
    >
      <Handle type="target" position={Position.Top} className="!size-1.5 !border-0 !bg-track" />
      <Marker state={data.state} n={data.n} size="sm" />
      <span className={cn("truncate text-xs", data.state === "pending" || data.state === "skipped" ? "text-muted-foreground" : "text-foreground")}>{data.title}</span>
      {data.count > 1 ? <span className="ml-auto rounded bg-muted px-1 text-[10px] text-muted-foreground">×{data.count}</span> : null}
      <Handle type="source" position={Position.Bottom} className="!size-1.5 !border-0 !bg-track" />
    </div>
  );
}

const nodeTypes = { step: FlowNode };

/** The real LangGraph graph: `_wait` halves fold into their checkpoint, hidden helper nodes are bridged over. */
export function GraphView({ wf, steps, execs, selected, onSelect }: { wf: Workflow; steps: Step[]; execs: Exec[]; selected?: string; onSelect: (id: string) => void }) {
  const { nodes, edges } = useMemo(() => {
    const g = wf.graph ?? { nodes: [], edges: [] };
    const byId = new Map(steps.map((s, i) => [s.id, { s, i }]));
    const hidden = new Set(wf.hidden_nodes ?? []);
    const ids = [...new Set(g.nodes.map((n) => baseId(n.id)))].filter((id) => !hidden.has(id));
    const counts: Record<string, number> = {};
    for (const e of execs) if (!e.node.endsWith("_wait")) counts[baseId(e.node)] = (counts[baseId(e.node)] ?? 0) + 1;

    // Edges between folded ids, without self loops (a checkpoint and its _wait half are one box).
    let raw = g.edges.map((e) => ({ s: baseId(e.source), t: baseId(e.target), c: e.conditional })).filter((e) => e.s !== e.t);
    // Bridge over hidden helper nodes (apply_*, schedule...): their predecessors connect straight to their successors.
    for (const h of hidden) {
      const ins = raw.filter((e) => e.t === h && e.s !== h);
      const outs = raw.filter((e) => e.s === h && e.t !== h);
      raw = raw.filter((e) => e.s !== h && e.t !== h);
      for (const i of ins) for (const o of outs) if (i.s !== o.t) raw.push({ s: i.s, t: o.t, c: i.c || o.c });
    }
    const seen = new Set<string>();
    const folded = raw.filter((e) => {
      const k = `${e.s}>${e.t}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });

    // Edges actually taken: consecutive executions in start order.
    const taken = new Set<string>();
    const order = execs.map((e) => baseId(e.node)).filter((n) => !hidden.has(n));
    for (let i = 1; i < order.length; i++) if (order[i - 1] !== order[i]) taken.add(`${order[i - 1]}>${order[i]}`);

    const dg = new dagre.graphlib.Graph();
    dg.setGraph({ rankdir: "TB", nodesep: 28, ranksep: 46 });
    dg.setDefaultEdgeLabel(() => ({}));
    for (const id of ids) dg.setNode(id, { width: 200, height: 40 });
    // Lay out along the main path (plain edges, the planned step order, and edges this run took); the other
    // conditional routes are still drawn, faintly, but do not pull the layout apart.
    const planned = (wf.steps ?? []).map(baseId);
    const main = (e: { s: string; t: string; c: boolean }) =>
      !e.c || taken.has(`${e.s}>${e.t}`) || planned.indexOf(e.t) === planned.indexOf(e.s) + 1;
    for (const e of folded) if (main(e)) dg.setEdge(e.s, e.t);
    dagre.layout(dg);

    const nodes: Node<NodeData>[] = ids.map((id) => {
      const p = dg.node(id);
      const hit = byId.get(id);
      const state: StepState = id === "__start__" || id === "__end__" ? (counts[id] || hit ? "done" : "pending") : hit?.s.state ?? (counts[id] ? "done" : "pending");
      return {
        id,
        type: "step",
        position: { x: p.x - 100, y: p.y - 20 },
        data: {
          title: id === "__start__" ? "Start" : id === "__end__" ? "End" : nodeTitle(wf, id),
          state,
          count: counts[id] ?? 0,
          n: hit ? hit.i + 1 : "·",
          selected: selected === id,
        },
      };
    });
    const edges: Edge[] = folded.map((e) => {
      const on = taken.has(`${e.s}>${e.t}`);
      return {
        id: `${e.s}>${e.t}`,
        source: e.s,
        target: e.t,
        animated: false,
        style: { stroke: on ? "var(--success)" : "var(--track)", strokeWidth: on ? 2 : 1.25, strokeDasharray: e.c && !on ? "4 4" : undefined, opacity: on || main(e) ? 1 : 0.35 },
        markerEnd: { type: MarkerType.ArrowClosed, color: on ? "var(--success)" : "var(--track)", width: 14, height: 14 },
      };
    });
    return { nodes, edges };
  }, [wf, steps, execs, selected]);

  return (
    <div className="h-[620px] rounded-xl border border-border bg-surface" data-testid="graph-view">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        defaultViewport={{ x: 0, y: 0, zoom: 0.8 }}
        onInit={(inst) => {
          const first = nodes.find((n) => n.id === "__start__") ?? nodes[0];
          if (first) inst.setCenter(first.position.x + 100, first.position.y + 220, { zoom: 0.8 });
        }}
        minZoom={0.2}
        nodesDraggable={false}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, n) => onSelect(n.id)}
      >
        <Background color="var(--track)" gap={20} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
