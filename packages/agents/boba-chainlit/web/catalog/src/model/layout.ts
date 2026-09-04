import ELK, { type ElkExtendedEdge, type ElkNode, type LayoutOptions } from "elkjs/lib/elk.bundled.js";

import type { NodePosition } from "./catalog";
import { measuredOf, type DatasetNode, type FlowEdge } from "./graph";

/** Раскладка ELK слева направо: партиция узла — номер слоя, поэтому источники
 * всегда левее приёмников; перенос из liam erd-core с партициями вместо групп. */
const LAYOUT_OPTIONS: LayoutOptions = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.partitioning.activate": "true",
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
  nodes: DatasetNode[];
  edges: FlowEdge[];
  partitionOf: (node: DatasetNode) => number;
  /** Сохранённые позиции вида: узел с позицией раскладка не двигает. */
  saved: NodePosition[];
};

/** Узлы с позициями: сохранённые как есть, остальные от ELK по размерам, которые
 * замерил React Flow. Скрытые и ещё не замеренные узлы в раскладке не участвуют. */
export async function computeLayout(input: LayoutInput): Promise<DatasetNode[]> {
  const savedById = new Map(input.saved.map((position) => [position.dataset_id, position]));
  const visible = input.nodes.filter((node) => !node.hidden && measuredOf(node) !== undefined);
  const visibleIds = new Set(visible.map((node) => node.id));
  const rest = input.nodes.filter((node) => !visibleIds.has(node.id));

  const children: ElkNode[] = visible.map((node) => {
    const size = measuredOf(node) ?? { width: 0, height: 0 };
    return {
      id: node.id,
      width: size.width,
      height: size.height,
      layoutOptions: {
        "elk.partitioning.partition": String(input.partitionOf(node)),
        "elk.alignment": "LEFT",
      },
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

  const positioned = visible.map((node) => {
    const saved = savedById.get(node.id);
    if (saved !== undefined) {
      return { ...node, position: { x: saved.x, y: saved.y } };
    }

    return { ...node, position: placed.get(node.id) ?? node.position };
  });

  return [...rest, ...positioned];
}
