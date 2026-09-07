import type { Edge, Node } from "@xyflow/react";

import {
  flowLabel,
  type Catalog,
  type ChangeStatus,
  type ColumnLink,
  type Flow,
  type Group,
  type NodeColumn,
  type Position,
  type ProcessNode,
  type Stale,
} from "./catalog";

/** Сколько колонок показывает карточка узла: все, только ключи, ничего. */
export type ShowMode = "ALL_FIELDS" | "KEY_ONLY" | "TABLE_NAME";

export const SHOW_MODES: ShowMode[] = ["ALL_FIELDS", "KEY_ONLY", "TABLE_NAME"];

export function isShowMode(value: string): value is ShowMode {
  return (SHOW_MODES as string[]).includes(value);
}

export type ProcessNodeData = {
  node: ProcessNode;
  label: string;
  group: Group | undefined;
  columns: NodeColumn[];
  showMode: ShowMode;
  status: ChangeStatus;
  showDiff: boolean;
  stale: Stale[];
  isActive: boolean;
  isHighlighted: boolean;
  /** Колонки, по которым идут подсвеченные линии. */
  litColumns: ReadonlySet<string>;
  /** На черновике карточку тянут за левый и правый край. */
  onResize: ((position: Position, width: number) => void) | undefined;
};

export type GroupNodeData = {
  group: Group;
  status: ChangeStatus;
  showDiff: boolean;
  count: number;
  /** Над рамкой тащат карточку: отпущенная войдёт в группу. */
  dropTarget: boolean;
  /** На черновике рамка несёт карандаш и корзину. */
  onRename: (() => void) | undefined;
  onRemove: (() => void) | undefined;
};

export type FlowEdgeData = {
  flow: Flow;
  /** Пара колонок этой линии; линия целого потока — без пары. */
  pair: ColumnLink | undefined;
  /** Ярлык потока несёт одна его линия; у остальных пусто. */
  label: string;
  status: ChangeStatus;
  showDiff: boolean;
  stale: Stale[];
  isHighlighted: boolean;
  /** Сдвиг середины линии от прямой между ручками: линию оттащили, чтобы
   * открыть то, что под ней. Живёт на странице, в процесс не пишется. */
  bend: Position | undefined;
  onBend: ((edgeId: string, bend: Position) => void) | undefined;
};

export type ProcessFlowNode = Node<ProcessNodeData, "process">;
export type GroupNode = Node<GroupNodeData, "group">;
export type FlowEdge = Edge<FlowEdgeData, "flow">;
export type CatalogNode = ProcessFlowNode | GroupNode;

/** Слои, которые режут порядок отрисовки: рамки групп под всем, линии под
 * карточками даже при подсветке — иначе линия перекрывала бы ручку, от
 * которой уже идёт, и новую линию от той же колонки было бы не потянуть. */
export const Z_INDEX = {
  frame: -1,
  node: 2,
  nodeHighlighted: 3,
  edge: 0,
  edgeHighlighted: 1,
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

/** Колонки карточки по режиму; в режиме ключей остаются и колонки, по
 * которым идут линии потоков, иначе линиям не к чему крепиться. */
export function visibleColumns(columns: NodeColumn[], showMode: ShowMode, involved: ReadonlySet<string>): NodeColumn[] {
  if (showMode === "ALL_FIELDS") {
    return columns;
  }

  if (showMode === "KEY_ONLY") {
    return columns.filter((column) => column.key || involved.has(column.name));
  }

  return [];
}

/** Ручка карточки целиком: линии потоков без пар и линии в режиме имён. */
export const NODE_HANDLE = "__node";

/** Ширина карточки без своей: та же, что --node-w в tokens.css; пределы
 * растягивания. Ширина стоит на узле React Flow явно, чтобы карточка
 * заполняла обёртку и тянулась вместе с ней. */
export const NODE_WIDTH = { default: 240, min: 160, max: 720 } as const;

/** Id линии пары: поток и номер пары в нём. */
export function pairEdgeId(flowId: string, index: number): string {
  return `${flowId}#${index}`;
}

/** Колонки узла, которые именуют его потоки на его стороне. */
export function involvedColumns(catalog: Catalog, nodeId: string): Set<string> {
  const involved = new Set<string>();
  const flows = catalog.flowsOf(nodeId);
  for (const flow of flows.outgoing) {
    for (const link of flow.columns) {
      involved.add(link.from_column);
    }
  }
  for (const flow of flows.incoming) {
    for (const link of flow.columns) {
      involved.add(link.to_column);
    }
  }
  return involved;
}

export const FRAME_PREFIX = "group:";

export function frameId(groupId: string): string {
  return `${FRAME_PREFIX}${groupId}`;
}

export function groupOfFrame(id: string): string | undefined {
  return id.startsWith(FRAME_PREFIX) ? id.slice(FRAME_PREFIX.length) : undefined;
}

export type GraphOptions = {
  showMode: ShowMode;
  showDiff: boolean;
  hidden: ReadonlySet<string>;
};

/** Узлы процесса и линии потоков из снимка. Позиция — из процесса; узел без
 * неё берёт запомненную авторазложенную, а без той стоит в нуле и ждёт
 * раскладку. Поток даёт линию на каждую пару колонок от ручки колонки к
 * ручке колонки; поток без пар и все потоки в режиме имён — одну линию
 * между карточками. Ярлык потока несёт его первая линия. */
export function buildGraph(
  catalog: Catalog,
  options: GraphOptions,
  autoPositions: ReadonlyMap<string, Position>,
): { nodes: ProcessFlowNode[]; edges: FlowEdge[] } {
  const members = catalog.nodes;
  const included = new Set(members.map((node) => node.id));
  const byColumns = options.showMode !== "TABLE_NAME";

  const nodes: ProcessFlowNode[] = members.map((node) => {
    const columns = visibleColumns(catalog.columnsOf(node.id), options.showMode, involvedColumns(catalog, node.id));
    return {
      id: node.id,
      type: "process",
      position: node.position ?? autoPositions.get(node.id) ?? { x: 0, y: 0 },
      width: node.width ?? NODE_WIDTH.default,
      hidden: options.hidden.has(node.id),
      zIndex: Z_INDEX.node,
      data: {
        node,
        label: catalog.label(node.id),
        group: catalog.group(node.group_id),
        columns,
        showMode: options.showMode,
        status: catalog.statusOf("node", node.id),
        showDiff: options.showDiff,
        stale: catalog.staleOf("node", node.id),
        isActive: false,
        isHighlighted: false,
        litColumns: new Set<string>(),
        onResize: undefined,
      },
    };
  });

  const edges: FlowEdge[] = [];
  for (const flow of catalog.flows) {
    if (!included.has(flow.from_node_id) || !included.has(flow.to_node_id)) {
      continue;
    }

    const hidden = options.hidden.has(flow.from_node_id) || options.hidden.has(flow.to_node_id);
    const common = {
      type: "flow" as const,
      source: flow.from_node_id,
      target: flow.to_node_id,
      hidden,
      zIndex: Z_INDEX.edge,
    };
    const shared = {
      flow,
      status: catalog.statusOf("flow", flow.id),
      showDiff: options.showDiff,
      stale: catalog.staleOf("flow", flow.id),
      isHighlighted: false,
      bend: undefined,
      onBend: undefined,
    };

    if (!byColumns || flow.columns.length === 0) {
      edges.push({
        ...common,
        id: flow.id,
        sourceHandle: NODE_HANDLE,
        targetHandle: NODE_HANDLE,
        data: { ...shared, pair: undefined, label: flowLabel(flow) },
      });
      continue;
    }

    flow.columns.forEach((pair, index) => {
      edges.push({
        ...common,
        id: pairEdgeId(flow.id, index),
        sourceHandle: pair.from_column,
        targetHandle: pair.to_column,
        data: { ...shared, pair, label: index === 0 ? flowLabel(flow) : "" },
      });
    });
  }

  return { nodes, edges };
}

/** Узлы, которым нужна раскладка: без позиции в процессе и без запомненной. */
export function unplaced(nodes: ProcessFlowNode[], autoPositions: ReadonlyMap<string, Position>): ProcessFlowNode[] {
  return nodes.filter((node) => !node.hidden && node.data.node.position === null && !autoPositions.has(node.id));
}

const FRAME_PADDING = 24;
const FRAME_TITLE = 32;
const EMPTY_FRAME = { width: 220, height: 96 };
const EMPTY_FRAME_GAP = 40;

export type FrameActions = {
  onRename: (group: Group) => void;
  onRemove: (group: Group) => void;
};

/** Как рисовать рамки: пустые группы стоят там, куда их оттащили (иначе
 * справа от занятых), рамка группы dropTarget подсвечена под карточкой. */
export type FrameLayout = {
  emptyPositions: ReadonlyMap<string, Position>;
  dropTarget: string | undefined;
};

/** Рамки групп под карточками: по крайним карточкам группы с отступом.
 * Пустая группа на черновике получает рамку справа от занятых (или там, куда
 * её оттащили), чтобы в неё можно было бросить карточку или снять её
 * корзиной; пустую рамку можно таскать. Узлы из except в расчёт не входят:
 * так рамка не тянется за перетаскиваемой карточкой. */
export function groupFrames(
  catalog: Catalog,
  nodes: ProcessFlowNode[],
  showDiff: boolean,
  actions: FrameActions | undefined,
  layout: FrameLayout,
  except: ReadonlySet<string> = new Set(),
): GroupNode[] {
  const frames: GroupNode[] = [];
  let rightEdge = 0;
  let topEdge = 0;
  for (const group of catalog.groups) {
    const members = nodes.filter(
      (node) =>
        !node.hidden && !except.has(node.id) && node.data.node.group_id === group.id && measuredOf(node) !== undefined,
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

    const x = left - FRAME_PADDING;
    const y = top - FRAME_PADDING - FRAME_TITLE;
    const width = right - left + FRAME_PADDING * 2;
    rightEdge = Math.max(rightEdge, x + width);
    topEdge = Math.min(topEdge, y);
    const height = bottom - top + FRAME_PADDING * 2 + FRAME_TITLE;
    frames.push(frame(group, catalog, showDiff, actions, layout, members.length, { x, y }, width, height));
  }

  if (actions === undefined) {
    return frames;
  }

  let x = rightEdge + EMPTY_FRAME_GAP;
  for (const group of catalog.groups) {
    if (frames.some((item) => item.data.group.id === group.id)) {
      continue;
    }

    const position = layout.emptyPositions.get(group.id) ?? { x, y: topEdge };
    frames.push(frame(group, catalog, showDiff, actions, layout, 0, position, EMPTY_FRAME.width, EMPTY_FRAME.height));
    x += EMPTY_FRAME.width + EMPTY_FRAME_GAP;
  }

  return frames;
}

function frame(
  group: Group,
  catalog: Catalog,
  showDiff: boolean,
  actions: FrameActions | undefined,
  layout: FrameLayout,
  count: number,
  position: Position,
  width: number,
  height: number,
): GroupNode {
  return {
    id: frameId(group.id),
    type: "group",
    position,
    width,
    height,
    zIndex: Z_INDEX.frame,
    draggable: count === 0 && actions !== undefined,
    selectable: false,
    connectable: false,
    data: {
      group,
      status: catalog.statusOf("group", group.id),
      showDiff,
      count,
      dropTarget: layout.dropTarget === group.id,
      onRename:
        actions === undefined
          ? undefined
          : () => {
              actions.onRename(group);
            },
      onRemove:
        actions === undefined
          ? undefined
          : () => {
              actions.onRemove(group);
            },
    },
  };
}

/** Группа, в рамку которой попала точка холста; мимо рамок — null. Рамка
 * группы except не считается: своя рамка обнимает карточку всегда. */
export function groupAt(frames: GroupNode[], x: number, y: number, except: string | null = null): string | null {
  for (const item of frames) {
    if (item.data.group.id === except) {
      continue;
    }

    const width = item.width ?? 0;
    const height = item.height ?? 0;
    const inside =
      x >= item.position.x && x <= item.position.x + width && y >= item.position.y && y <= item.position.y + height;
    if (inside) {
      return groupOfFrame(item.id) ?? null;
    }
  }

  return null;
}

/** Центр карточки: по нему решается, в какой рамке она стоит. */
export function centerOf(node: ProcessFlowNode): Position {
  const size = measuredOf(node) ?? { width: 0, height: 0 };
  return { x: node.position.x + size.width / 2, y: node.position.y + size.height / 2 };
}

const FREE_GAP = 80;

/** Место для новой карточки без точки на холсте: справа от крайней. */
export function freePosition(nodes: ProcessFlowNode[]): Position {
  let right = Number.NEGATIVE_INFINITY;
  let top = Number.POSITIVE_INFINITY;
  for (const node of nodes) {
    if (node.hidden) {
      continue;
    }

    const size = measuredOf(node) ?? { width: 0, height: 0 };
    right = Math.max(right, node.position.x + size.width);
    top = Math.min(top, node.position.y);
  }

  if (!Number.isFinite(right)) {
    return { x: 0, y: 0 };
  }

  return { x: right + FREE_GAP, y: top };
}
