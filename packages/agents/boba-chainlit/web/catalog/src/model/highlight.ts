import { Z_INDEX, type FlowEdge, type ProcessFlowNode } from "./graph";

export type HighlightTrigger = {
  activeId: string | undefined;
  hoverId: string | undefined;
};

/** Подсветка цепочки: активный узел, его соседи по потокам и рёбра к ним;
 * hover подсвечивает так же, но без пометки активного. Перенос из liam erd-core. */
export function highlight(
  nodes: ProcessFlowNode[],
  edges: FlowEdge[],
  trigger: HighlightTrigger,
): { nodes: ProcessFlowNode[]; edges: FlowEdge[] } {
  const related = new Map<string, Set<string>>();
  for (const edge of edges) {
    if (!related.has(edge.source)) {
      related.set(edge.source, new Set());
    }
    if (!related.has(edge.target)) {
      related.set(edge.target, new Set());
    }
    related.get(edge.source)?.add(edge.target);
    related.get(edge.target)?.add(edge.source);
  }

  const isRelated = (target: string | undefined, id: string): boolean => {
    if (target === undefined) {
      return false;
    }

    return related.get(target)?.has(id) ?? false;
  };

  const highlightedNodes = nodes.map((node) => {
    const isActive = node.id === trigger.activeId;
    const isHighlighted =
      isRelated(trigger.activeId, node.id) || node.id === trigger.hoverId || isRelated(trigger.hoverId, node.id);

    return {
      ...node,
      zIndex: isActive || isHighlighted ? Z_INDEX.nodeHighlighted : Z_INDEX.node,
      data: { ...node.data, isActive, isHighlighted },
    };
  });

  const touches = (target: string | undefined, edge: FlowEdge): boolean =>
    target !== undefined && (edge.source === target || edge.target === target);

  const highlightedEdges = edges.map((edge) => {
    const isHighlighted = touches(trigger.activeId, edge) || touches(trigger.hoverId, edge);
    const data = edge.data;
    if (data === undefined) {
      return edge;
    }

    return {
      ...edge,
      zIndex: isHighlighted ? Z_INDEX.edgeHighlighted : Z_INDEX.edge,
      data: { ...data, isHighlighted },
    };
  });

  return { nodes: highlightedNodes, edges: highlightedEdges };
}
