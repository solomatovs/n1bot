import dagre from "@dagrejs/dagre";

import type { Edge, EdgeKind, Stage, WorkflowGraph } from "./workflow";

/** Раскладка графа запуска в два уровня: задачи внутри стадии, стадии между собой.
 *  Чистая логика без React: результат — прямоугольники в координатах канваса. */

export type Point = { x: number; y: number };
export type Size = { width: number; height: number };

export type TaskBox = {
  task: string;
  stage: string;
  /** Позиция относительно стадии — так её ждёт React Flow у дочернего узла. */
  position: Point;
  size: Size;
};

export type StageBox = {
  stage: string;
  position: Point;
  size: Size;
};

export type LaidEdge = {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  /** Подпись ребра: аргумент у value, порт у stream, пусто у control. */
  label: string;
};

export type Layout = {
  stages: StageBox[];
  tasks: TaskBox[];
  edges: LaidEdge[];
};

export const TASK_SIZE: Size = { width: 200, height: 64 };

const STAGE_PADDING = 24;
const STAGE_HEADER = 28;
const TASK_GAP = 16;
const STAGE_GAP = 56;

type Direction = "LR" | "TB";

function dagreGraph(direction: Direction, nodesep: number, ranksep: number): dagre.graphlib.Graph {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: direction, nodesep, ranksep, marginx: 0, marginy: 0 });
  graph.setDefaultEdgeLabel(() => ({}));
  return graph;
}

function stageOf(graph: WorkflowGraph): Map<string, Stage> {
  const byTask = new Map<string, Stage>();
  for (const stage of graph.stages) {
    for (const task of stage.tasks) {
      byTask.set(task, stage);
    }
  }

  return byTask;
}

/** Раскладка задач одной стадии: потоковые рёбра задают порядок слева направо. */
function layoutStage(stage: Stage, edges: Edge[]): { tasks: TaskBox[]; size: Size } {
  const inner = dagreGraph("LR", TASK_GAP, TASK_GAP * 2);
  for (const task of stage.tasks) {
    inner.setNode(task, { ...TASK_SIZE });
  }

  const members = new Set(stage.tasks);
  for (const edge of edges) {
    if (members.has(edge.src.task) && members.has(edge.dst.task)) {
      inner.setEdge(edge.src.task, edge.dst.task);
    }
  }

  dagre.layout(inner);

  let maxX = 0;
  let maxY = 0;
  const tasks: TaskBox[] = [];
  for (const task of stage.tasks) {
    const node = inner.node(task);
    // dagre отдаёт центр узла; React Flow ждёт левый верхний угол
    const x = node.x - TASK_SIZE.width / 2 + STAGE_PADDING;
    const y = node.y - TASK_SIZE.height / 2 + STAGE_PADDING + STAGE_HEADER;
    tasks.push({ task, stage: stage.id, position: { x, y }, size: { ...TASK_SIZE } });
    maxX = Math.max(maxX, x + TASK_SIZE.width);
    maxY = Math.max(maxY, y + TASK_SIZE.height);
  }

  return { tasks, size: { width: maxX + STAGE_PADDING, height: maxY + STAGE_PADDING } };
}

function edgeLabel(edge: Edge): string {
  switch (edge.kind) {
    case "value":
      return edge.dst.name;
    case "stream":
      return `${edge.src.name} → ${edge.dst.name}`;
    case "control":
      return "";
  }
}

function laidEdges(graph: WorkflowGraph): LaidEdge[] {
  return graph.spec.edges.map((edge, index) => ({
    id: `e${index}:${edge.src.task}->${edge.dst.task}`,
    source: edge.src.task,
    target: edge.dst.task,
    kind: edge.kind,
    label: edgeLabel(edge),
  }));
}

export function layoutGraph(graph: WorkflowGraph): Layout {
  const byTask = stageOf(graph);
  const inner = new Map<string, { tasks: TaskBox[]; size: Size }>();
  const outer = dagreGraph("LR", STAGE_GAP, STAGE_GAP);

  for (const stage of graph.stages) {
    const laid = layoutStage(stage, graph.spec.edges);
    inner.set(stage.id, laid);
    outer.setNode(stage.id, { ...laid.size });
  }

  // порядок стадий: зависимости стадий плюс рёбра между задачами разных стадий
  for (const stage of graph.stages) {
    for (const dependency of stage.after) {
      outer.setEdge(dependency, stage.id);
    }
  }
  for (const edge of graph.spec.edges) {
    const from = byTask.get(edge.src.task);
    const to = byTask.get(edge.dst.task);
    if (from !== undefined && to !== undefined && from.id !== to.id) {
      outer.setEdge(from.id, to.id);
    }
  }

  dagre.layout(outer);

  const stages: StageBox[] = [];
  const tasks: TaskBox[] = [];
  for (const stage of graph.stages) {
    const laid = inner.get(stage.id);
    if (laid === undefined) {
      continue;
    }

    const node = outer.node(stage.id);
    stages.push({
      stage: stage.id,
      position: { x: node.x - laid.size.width / 2, y: node.y - laid.size.height / 2 },
      size: laid.size,
    });
    tasks.push(...laid.tasks);
  }

  return { stages, tasks, edges: laidEdges(graph) };
}

export type TaskPositions = Record<string, Point>;

export type TaskSizes = Record<string, Size>;

/** Плоская раскладка задач редактора слева направо по рёбрам; без стадий.
 *  Размер узла — по задаче: у узла редактора он растёт с числом портов. */
export function layoutTasks(
  tasks: readonly string[],
  edges: readonly { source: string; target: string }[],
  sizes: TaskSizes = {},
): TaskPositions {
  const graph = dagreGraph("LR", TASK_GAP * 2, STAGE_GAP);
  for (const task of tasks) {
    graph.setNode(task, { ...(sizes[task] ?? TASK_SIZE) });
  }

  const known = new Set(tasks);
  for (const edge of edges) {
    if (known.has(edge.source) && known.has(edge.target)) {
      graph.setEdge(edge.source, edge.target);
    }
  }

  dagre.layout(graph);

  const positions: TaskPositions = {};
  for (const task of tasks) {
    const node = graph.node(task);
    const size = sizes[task] ?? TASK_SIZE;
    positions[task] = { x: node.x - size.width / 2, y: node.y - size.height / 2 };
  }

  return positions;
}
