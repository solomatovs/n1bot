import type { Edge, Node } from "@xyflow/react";

import type { Catalog, ChangeStatus, Column, Dataset, Flow, Layer } from "./catalog";

/** Сколько колонок показывает карточка набора: все, только ключи, ничего. */
export type ShowMode = "ALL_FIELDS" | "KEY_ONLY" | "TABLE_NAME";

export const SHOW_MODES: ShowMode[] = ["ALL_FIELDS", "KEY_ONLY", "TABLE_NAME"];

export function isShowMode(value: string): value is ShowMode {
  return (SHOW_MODES as string[]).includes(value);
}

export type DatasetNodeData = {
  dataset: Dataset;
  layer: Layer | undefined;
  columns: Column[];
  showMode: ShowMode;
  status: ChangeStatus;
  showDiff: boolean;
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
  isHighlighted: boolean;
};

export type DatasetNode = Node<DatasetNodeData, "dataset">;
export type LayerNode = Node<LayerNodeData, "layer">;
export type FlowEdge = Edge<FlowEdgeData, "flow">;
export type CatalogNode = DatasetNode | LayerNode;

/** Слои, которые режут порядок отрисовки: дорожки под карточками, рёбра над. */
export const Z_INDEX = {
  lane: -1,
  node: 1,
  nodeHighlighted: 2,
  edge: 0,
  edgeHighlighted: 3,
} as const;

/** Размер карточки набора без замера DOM: раскладка считается до первого кадра. */
export const NODE_SIZE = {
  width: 240,
  header: 44,
  row: 22,
  footer: 8,
} as const;

export function nodeHeight(columns: number, showMode: ShowMode): number {
  if (showMode === "TABLE_NAME") {
    return NODE_SIZE.header;
  }

  return NODE_SIZE.header + columns * NODE_SIZE.row + NODE_SIZE.footer;
}

export function visibleColumns(columns: Column[], showMode: ShowMode): Column[] {
  if (showMode === "ALL_FIELDS") {
    return columns;
  }

  if (showMode === "KEY_ONLY") {
    return columns.filter((column) => column.is_key);
  }

  return [];
}

export const LANE_PREFIX = "layer:";

export function laneId(layerId: string): string {
  return `${LANE_PREFIX}${layerId}`;
}

export type GraphOptions = {
  showMode: ShowMode;
  showDiff: boolean;
  /** Наборы вида; пусто — весь каталог. */
  datasetIds: ReadonlySet<string>;
  /** Слои вида; пусто — все. */
  layerIds: ReadonlySet<string>;
  hidden: ReadonlySet<string>;
};

/** Наборы, попавшие в вид: фильтр по слоям и по списку наборов; удалённые в
 * черновике остаются, их показывает diff. */
export function datasetsInView(catalog: Catalog, options: GraphOptions): Dataset[] {
  return catalog.datasets.filter((dataset) => {
    if (options.layerIds.size > 0 && !options.layerIds.has(dataset.layer_id)) {
      return false;
    }

    return options.datasetIds.size === 0 || options.datasetIds.has(dataset.id);
  });
}

/** Узлы наборов и рёбра потоков из снимка; позиции нулевые — их ставит раскладка. */
export function buildGraph(catalog: Catalog, options: GraphOptions): { nodes: DatasetNode[]; edges: FlowEdge[] } {
  const datasets = datasetsInView(catalog, options);
  const included = new Set(datasets.map((dataset) => dataset.id));

  const nodes: DatasetNode[] = datasets.map((dataset) => {
    const columns = visibleColumns(catalog.columnsOf(dataset.id), options.showMode);
    return {
      id: dataset.id,
      type: "dataset",
      position: { x: 0, y: 0 },
      hidden: options.hidden.has(dataset.id),
      zIndex: Z_INDEX.node,
      width: NODE_SIZE.width,
      height: nodeHeight(columns.length, options.showMode),
      data: {
        dataset,
        layer: catalog.layer(dataset.layer_id),
        columns,
        showMode: options.showMode,
        status: catalog.statusOf("dataset", dataset.id),
        showDiff: options.showDiff,
        isActive: false,
        isHighlighted: false,
      },
    };
  });

  const edges: FlowEdge[] = [];
  for (const flow of catalog.flows) {
    if (!included.has(flow.from_dataset_id) || !included.has(flow.to_dataset_id)) {
      continue;
    }

    edges.push({
      id: flow.id,
      type: "flow",
      source: flow.from_dataset_id,
      target: flow.to_dataset_id,
      hidden: options.hidden.has(flow.from_dataset_id) || options.hidden.has(flow.to_dataset_id),
      zIndex: Z_INDEX.edge,
      data: {
        flow,
        loadKind: catalog.loadKindName(flow),
        status: catalog.statusOf("flow", flow.id),
        showDiff: options.showDiff,
        isHighlighted: false,
      },
    });
  }

  return { nodes, edges };
}

const LANE_PADDING = 24;
const LANE_TITLE = 32;

/** Дорожки слоёв под разложенными узлами: рамка по крайним карточкам слоя. */
export function laneNodes(catalog: Catalog, nodes: DatasetNode[], showDiff: boolean): LayerNode[] {
  const lanes: LayerNode[] = [];
  for (const layer of catalog.layers) {
    const members = nodes.filter((node) => !node.hidden && node.data.dataset.layer_id === layer.id);
    if (members.length === 0) {
      continue;
    }

    let left = Number.POSITIVE_INFINITY;
    let top = Number.POSITIVE_INFINITY;
    let right = Number.NEGATIVE_INFINITY;
    let bottom = Number.NEGATIVE_INFINITY;
    for (const node of members) {
      const width = node.width ?? NODE_SIZE.width;
      const height = node.height ?? NODE_SIZE.header;
      left = Math.min(left, node.position.x);
      top = Math.min(top, node.position.y);
      right = Math.max(right, node.position.x + width);
      bottom = Math.max(bottom, node.position.y + height);
    }

    lanes.push({
      id: laneId(layer.id),
      type: "layer",
      position: { x: left - LANE_PADDING, y: top - LANE_PADDING - LANE_TITLE },
      width: right - left + LANE_PADDING * 2,
      height: bottom - top + LANE_PADDING * 2 + LANE_TITLE,
      zIndex: Z_INDEX.lane,
      draggable: false,
      selectable: false,
      connectable: false,
      data: {
        layer,
        status: catalog.statusOf("layer", layer.id),
        showDiff,
        count: members.length,
      },
    });
  }

  return lanes;
}
