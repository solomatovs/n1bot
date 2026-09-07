import ELK, { type ElkExtendedEdge, type ElkNode, type LayoutOptions } from "elkjs/lib/elk.bundled.js";

import { measuredOf, type FlowEdge, type ProcessFlowNode } from "./graph";

/** Раскладка ELK слева направо по потокам: источники левее приёмников;
 * перенос из liam erd-core. */
const LAYOUT_OPTIONS: LayoutOptions = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.separateConnectedComponents": "false",
  "elk.layered.spacing.baseValue": "40",
  "elk.spacing.componentComponent": "80",
  "elk.layered.spacing.edgeNodeBetweenLayers": "120",
  "elk.layered.spacing.nodeNodeBetweenLayers": "160",
  "elk.layered.considerModelOrder.strategy": "PREFER_EDGES",
  "elk.layered.crossingMinimization.forceNodeModelOrder": "true",
  "elk.layered.mergeEdges": "true",
};

const elk = new ELK();

export type LayoutInput = {
  nodes: ProcessFlowNode[];
  edges: FlowEdge[];
};

/** Позиции от ELK по размерам, которые замерил React Flow, для видимых и
 * замеренных узлов; кто из них переедет, решает холст. */
export async function computeLayout(input: LayoutInput): Promise<Map<string, { x: number; y: number }>> {
  const visible = input.nodes.filter((node) => !node.hidden && measuredOf(node) !== undefined);
  const visibleIds = new Set(visible.map((node) => node.id));

  const children: ElkNode[] = visible.map((node) => {
    const size = measuredOf(node) ?? { width: 0, height: 0 };
    return {
      id: node.id,
      width: size.width,
      height: size.height,
      layoutOptions: { "elk.alignment": "LEFT" },
    };
  });

  const edges: ElkExtendedEdge[] = input.edges
    .filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target))
    .map((edge) => ({ id: edge.id, sources: [edge.source], targets: [edge.target] }));

  const layout = await elk.layout({ id: "root", layoutOptions: LAYOUT_OPTIONS, children, edges });
  const placed = new Map<string, { x: number; y: number }>();
  for (const child of layout.children ?? []) {
    placed.set(child.id, { x: child.x ?? 0, y: child.y ?? 0 });
  }

  return placed;
}
