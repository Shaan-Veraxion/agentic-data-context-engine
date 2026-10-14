"use client";

import "@xyflow/react/dist/style.css";
import { Background, Controls, MarkerType, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { use, useEffect, useMemo, useState } from "react";
import { AppTabs } from "@/components/AppTabs";
import { api, type LineageEdge, type LineageNode } from "@/lib/api";

const COLORS: Record<LineageNode["type"], string> = { source: "#8a8578", qvd: "#2e8b6e", mapping: "#c98a1b", table: "#6a5acd" };
const COL_W = 300;
const ROW_H = 84;

// Left-to-right layout: each node's column is its dependency depth (sources and input QVDs first, stored QVDs last).
function layout(nodes: LineageNode[], edges: LineageEdge[]): Node[] {
  const incoming = new Map<string, string[]>();
  for (const e of edges) incoming.set(e.target, [...(incoming.get(e.target) ?? []), e.source]);
  const depth = new Map<string, number>();
  const visit = (id: string, seen: Set<string>): number => {
    if (depth.has(id)) return depth.get(id)!;
    if (seen.has(id)) return 0;
    seen.add(id);
    const ins = incoming.get(id) ?? [];
    const d = ins.length ? 1 + Math.max(...ins.map((s) => visit(s, seen))) : 0;
    depth.set(id, d);
    return d;
  };
  nodes.forEach((n) => visit(n.id, new Set()));
  const rows = new Map<number, number>();
  return nodes
    .slice()
    .sort((a, b) => (depth.get(a.id)! - depth.get(b.id)!) || a.label.localeCompare(b.label))
    .map((n) => {
      const c = depth.get(n.id) ?? 0;
      const r = rows.get(c) ?? 0;
      rows.set(c, r + 1);
      const label = `${n.label}${n.level ? ` · ${n.level}` : ""}${n.dropped ? " (dropped)" : ""}`;
      return {
        id: n.id,
        position: { x: c * COL_W, y: r * ROW_H },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: { label },
        style: { border: `1.5px solid ${COLORS[n.type]}`, borderRadius: 8, padding: 8, width: 220, fontSize: 12,
                 opacity: n.dropped ? 0.6 : 1 },
      };
    });
}

export default function Lineage({ params }: { params: Promise<{ appId: string }> }) {
  const { appId } = use(params);
  const [graph, setGraph] = useState<{ nodes: LineageNode[]; edges: LineageEdge[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.lineage(appId).then(setGraph).catch((e: Error) => setError(e.message));
  }, [appId]);

  const flow = useMemo(() => {
    if (!graph) return null;
    const nodes = layout(graph.nodes, graph.edges);
    const edges: Edge[] = graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.via ?? (e.kind === "stores_qvd" ? "store" : e.kind),
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { strokeDasharray: e.kind === "stores_qvd" ? "4 3" : undefined },
    }));
    return { nodes, edges };
  }, [graph]);

  return (
    <>
      <h1>{appId}</h1>
      <AppTabs app={appId} on="lineage" />
      <div className="row muted">
        <span><span className="chip" style={{ borderLeft: `3px solid ${COLORS.source}` }}>Dremio source</span></span>
        <span><span className="chip" style={{ borderLeft: `3px solid ${COLORS.table}` }}>Qlik table · level</span></span>
        <span><span className="chip" style={{ borderLeft: `3px solid ${COLORS.mapping}` }}>mapping table</span></span>
        <span><span className="chip" style={{ borderLeft: `3px solid ${COLORS.qvd}` }}>QVD</span></span>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="panel" style={{ height: 560, padding: 0 }}>
        {flow ? (
          <ReactFlow nodes={flow.nodes} edges={flow.edges} fitView proOptions={{ hideAttribution: true }}>
            <Background />
            <Controls />
          </ReactFlow>
        ) : (
          <p className="muted" style={{ padding: 16 }}>Loading…</p>
        )}
      </div>
    </>
  );
}
