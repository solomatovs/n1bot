import type { Edge, Node } from "@xyflow/react";

import type { Catalog, ChangeStatus, Flow, Layer, NodeColumn, ProcessNode, Stale } from "./catalog";

/** Сколько колонок показывает карточка узла: все, только ключи, ничего. */
export type ShowMode = "ALL_FIELDS" | "KEY_ONLY" | "TABLE_NAME";

export const SHOW_MODES: ShowMode[] = ["ALL_FIELDS", "KEY_ONLY", "TABLE_NAME"];

export function isShowMode(value: string): value is ShowMode {
  return (SHOW_MODES as string[]).includes(value);
}

export type ProcessNodeData = {
  node: ProcessNode;
  label: string;
  layer: Layer | undefined;
  columns: NodeColumn[];
  showMode: ShowMode;
  status: ChangeStatus;
  showDiff: boolean;
  stale: Stale[];
  isActive: boolean;
  isHighlighted: boolean;
};

export type LayerNodeData = {
  layer: Layer;
  status: ChangeStatus;
  showDiff: boolean;
  count: number;
};

export type FlowEdgeData = {
  flow: Flow;
  loadKind: string;
  status: ChangeStatus;
  showDiff: boolean;
  stale: Stale[];
  isHighlighted: boolean;
};

export type ProcessFlowNode = Node<ProcessNodeData, "process">;
export type LayerNode = Node<LayerNodeData, "layer">;
export type FlowEdge = Edge<FlowEdgeData, "flow">;
export type CatalogNode = ProcessFlowNode | LayerNode;

/** Слои, которые режут порядок отрисовки: дорожки под карточками, рёбра над. */
export const Z_INDEX = {
  lane: -1,
  node: 1,
  nodeHighlighted: 2,
  edge: 0,
  edgeHighlighted: 3,
} as const;

/** Размер узла по замеру React Flow; до замера узел не участвует в раскладке. */
export type Measured = { width: number; height: number };

export function measuredOf(node: Node): Measured | undefined {
  const width = node.measured?.width;
  const height = node.measured?.height;
  if (width === undefined || height === undefined) {
    return undefined;
  }

  return { width, height };
}

export function visibleColumns(columns: NodeColumn[], showMode: ShowMode): NodeColumn[] {
  if (showMode === "ALL_FIELDS") {
    return columns;
  }

  if (showMode === "KEY_ONLY") {
    return columns.filter((column) => column.key);
  }

  return [];
}

export const LANE_PREFIX = "layer:";

export function laneId(layerId: string): string {
  return `${LANE_PREFIX}${layerId}`;
}

export function layerOfLane(id: string): string | undefined {
  return id.startsWith(LANE_PREFIX) ? id.slice(LANE_PREFIX.length) : undefined;
}

export type GraphOptions = {
  showMode: ShowMode;
  showDiff: boolean;
  /** Узлы вида; пусто — весь процесс. */
  nodeIds: ReadonlySet<string>;
  /** Слои вида; пусто — все. */
  layerIds: ReadonlySet<string>;
  hidden: ReadonlySet<string>;
};

/** Узлы, попавшие в вид: фильтр по слоям и по списку узлов; удалённые в
 * черновике остаются, их показывает diff. */
export function nodesInView(catalog: Catalog, options: GraphOptions): ProcessNode[] {
  return catalog.nodes.filter((node) => {
    if (options.layerIds.size > 0 && !options.layerIds.has(node.layer_id)) {
      return false;
    }

    return options.nodeIds.size === 0 || options.nodeIds.has(node.id);
  });
}

/** Узлы процесса и рёбра потоков из снимка; позиции нулевые — их ставит раскладка. */
export function buildGraph(catalog: Catalog, options: GraphOptions): { nodes: ProcessFlowNode[]; edges: FlowEdge[] } {
  const members = nodesInView(catalog, options);
  const included = new Set(members.map((node) => node.id));

  const nodes: ProcessFlowNode[] = members.map((node) => {
    const columns = visibleColumns(catalog.columnsOf(node.id), options.showMode);
    return {
      id: node.id,
      type: "process",
      position: { x: 0, y: 0 },
      hidden: options.hidden.has(node.id),
      zIndex: Z_INDEX.node,
      data: {
        node,
        label: catalog.label(node.id),
        layer: catalog.layer(node.layer_id),
        columns,
        showMode: options.showMode,
        status: catalog.statusOf("node", node.id),
        showDiff: options.showDiff,
        stale: catalog.staleOf("node", node.id),
        isActive: false,
        isHighlighted: false,
      },
    };
  });

  const edges: FlowEdge[] = [];
  for (const flow of catalog.flows) {
    if (!included.has(flow.from_node_id) || !included.has(flow.to_node_id)) {
      continue;
    }

    edges.push({
      id: flow.id,
      type: "flow",
      source: flow.from_node_id,
      target: flow.to_node_id,
      hidden: options.hidden.has(flow.from_node_id) || options.hidden.has(flow.to_node_id),
      zIndex: Z_INDEX.edge,
      data: {
        flow,
        loadKind: catalog.loadKindName(flow),
        status: catalog.statusOf("flow", flow.id),
        showDiff: options.showDiff,
        stale: catalog.staleOf("flow", flow.id),
        isHighlighted: false,
      },
    });
  }

  return { nodes, edges };
}

const LANE_PADDING = 24;
const LANE_TITLE = 32;
const EMPTY_LANE = { width: 220, height: 96 };
const EMPTY_LANE_GAP = 40;

/** Дорожки слоёв под разложенными узлами: рамка по крайним карточкам слоя.
 * Пустой слой на черновике получает пустую дорожку справа от занятых, чтобы в
 * неё можно было бросить объект. */
export function laneNodes(
  catalog: Catalog,
  nodes: ProcessFlowNode[],
  showDiff: boolean,
  withEmpty: boolean,
): LayerNode[] {
  const lanes: LayerNode[] = [];
  let rightEdge = 0;
  let topEdge = 0;
  for (const layer of catalog.layers) {
    const members = nodes.filter(
      (node) => !node.hidden && node.data.node.layer_id === layer.id && measuredOf(node) !== undefined,
    );
    if (members.length === 0) {
      continue;
    }

    let left = Number.POSITIVE_INFINITY;
    let top = Number.POSITIVE_INFINITY;
    let right = Number.NEGATIVE_INFINITY;
    let bottom = Number.NEGATIVE_INFINITY;
    for (const node of members) {
      const size = measuredOf(node);
      if (size === undefined) {
        continue;
      }

      const { width, height } = size;
      left = Math.min(left, node.position.x);
      top = Math.min(top, node.position.y);
      right = Math.max(right, node.position.x + width);
      bottom = Math.max(bottom, node.position.y + height);
    }

    const x = left - LANE_PADDING;
    const y = top - LANE_PADDING - LANE_TITLE;
    const width = right - left + LANE_PADDING * 2;
    rightEdge = Math.max(rightEdge, x + width);
    topEdge = Math.min(topEdge, y);
    lanes.push(
      lane(layer, catalog, showDiff, members.length, x, y, width, bottom - top + LANE_PADDING * 2 + LANE_TITLE),
    );
  }

  if (!withEmpty) {
    return lanes;
  }

  let x = rightEdge + EMPTY_LANE_GAP;
  for (const layer of catalog.layers) {
    if (lanes.some((item) => item.data.layer.id === layer.id)) {
      continue;
    }

    lanes.push(lane(layer, catalog, showDiff, 0, x, topEdge, EMPTY_LANE.width, EMPTY_LANE.height));
    x += EMPTY_LANE.width + EMPTY_LANE_GAP;
  }

  return lanes;
}

function lane(
  layer: Layer,
  catalog: Catalog,
  showDiff: boolean,
  count: number,
  x: number,
  y: number,
  width: number,
  height: number,
): LayerNode {
  return {
    id: laneId(layer.id),
    type: "layer",
    position: { x, y },
    width,
    height,
    zIndex: Z_INDEX.lane,
    draggable: false,
    selectable: false,
    connectable: false,
    data: {
      layer,
      status: catalog.statusOf("layer", layer.id),
      showDiff,
      count,
    },
  };
}
