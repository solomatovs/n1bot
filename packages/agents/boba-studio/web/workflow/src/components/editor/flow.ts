import { MarkerType, Position, type Connection, type Edge as FlowEdge, type NodeHandle } from "@xyflow/react";

import { blockRows } from "../../model/args";
import type { TaskPositions } from "../../model/layout";
import { edgeId, edgeKindOf, type EditableEdge, type EditableTask, type EditableWorkflow } from "../../model/spec";
import type { ToolCatalog } from "../../model/workflow";
import { phaseColor } from "../../model/summary";
import { sideHandle } from "../graph/geometry";
import type { EditorStageFlowNode } from "./EditorStageNode";
import { editorNodeHeight, editorPorts, type EditorTaskData, type EditorTaskFlowNode } from "./EditorTaskNode";
import { portOfHandle } from "./handles";

/** Модель редактора → узлы и рёбра React Flow и обратно из жестов пользователя. */

export const EDITOR_NODE_WIDTH = 260;

const EDGE_COLOR = {
  control: "var(--edge-control)",
  value: "var(--edge-value)",
  stream: "var(--edge-stream)",
} as const;

export function taskData(
  task: EditableTask,
  catalog: ToolCatalog,
  edges: EditableEdge[],
  selected: string | null,
  issue: string,
): EditorTaskData {
  const facts = catalog[task.tool];
  const rows = blockRows(task, facts, edges);

  const readPorts: string[] = [];
  const writePorts: string[] = [];
  for (const [name, direction] of Object.entries(task.ports)) {
    (direction === "read" ? readPorts : writePorts).push(name);
  }
  for (const port of facts?.ports ?? []) {
    (port.direction === "read" ? readPorts : writePorts).push(port.name);
  }

  return {
    name: task.name,
    tool: task.tool,
    intent: rows.intent,
    rows: rows.body,
    readPorts,
    writePorts,
    results: facts?.results ?? [],
    selected: task.name === selected,
    issue,
  };
}

export function editorNodes(
  workflow: EditableWorkflow,
  positions: TaskPositions,
  catalog: ToolCatalog,
  selected: string | null,
  issues: Map<string, string>,
): EditorTaskFlowNode[] {
  return workflow.tasks.map((task) => {
    const data = taskData(task, catalog, workflow.edges, selected, issues.get(task.name) ?? "");
    return {
      id: task.name,
      type: "editorTask",
      position: positions[task.name] ?? { x: 0, y: 0 },
      width: EDITOR_NODE_WIDTH,
      height: editorNodeHeight(data),
      handles: editorHandles(data),
      data,
    };
  });
}

function editorHandles(data: EditorTaskData): NodeHandle[] {
  const ports = editorPorts(data);
  const handles: NodeHandle[] = [];
  for (const port of [ports.taskIn, ...ports.args, ...ports.reads]) {
    handles.push(sideHandle("target", Position.Left, 0, port.top, port.id));
  }
  for (const port of [ports.taskOut, ...ports.writes, ports.result]) {
    handles.push(sideHandle("source", Position.Right, EDITOR_NODE_WIDTH, port.top, port.id));
  }

  return handles;
}

export function editorEdges(workflow: EditableWorkflow): FlowEdge[] {
  return workflow.edges.map((edge) => {
    const kind = edgeKindOf(edge.src.kind, edge.dst.kind) ?? "control";
    const color = EDGE_COLOR[kind];
    return {
      id: edgeId(edge),
      source: edge.src.task,
      sourceHandle: `out:${edge.src.kind}${edge.src.name === "" ? "" : `:${edge.src.name}`}`,
      target: edge.dst.task,
      targetHandle: `in:${edge.dst.kind}${edge.dst.name === "" ? "" : `:${edge.dst.name}`}`,
      label: kind === "value" ? edge.dst.name : kind === "stream" ? `${edge.src.name} → ${edge.dst.name}` : undefined,
      style: { stroke: color, strokeDasharray: kind === "control" ? "" : "6 4" },
      markerEnd: { type: MarkerType.ArrowClosed, color },
    };
  });
}

const STAGE_PAD = 18;
const STAGE_HEAD = 40;

/** Цепочки задач по потоковым рёбрам — будущие стадии исполнения.
 * Порядок внутри цепочки — по направлению потока; при ветвлении или цикле
 * (невалидная спека посреди правки) — порядок появления задач. */
export function streamChains(workflow: EditableWorkflow): string[][] {
  const next = new Map<string, string>();
  const fed = new Set<string>();
  const linked = new Set<string>();
  let linear = true;

  for (const edge of workflow.edges) {
    if (edge.src.kind !== "fd" || edge.dst.kind !== "fd") {
      continue;
    }

    if (next.has(edge.src.task) || fed.has(edge.dst.task)) {
      linear = false;
    }

    next.set(edge.src.task, edge.dst.task);
    fed.add(edge.dst.task);
    linked.add(edge.src.task);
    linked.add(edge.dst.task);
  }

  const seen = new Set<string>();
  const chains: string[][] = [];
  for (const task of workflow.tasks) {
    const name = task.name;
    if (!linked.has(name) || seen.has(name) || fed.has(name)) {
      continue;
    }

    const chain: string[] = [];
    let cursor: string | undefined = name;
    while (cursor !== undefined && !seen.has(cursor)) {
      chain.push(cursor);
      seen.add(cursor);
      cursor = next.get(cursor);
    }

    chains.push(chain);
  }

  if (!linear) {
    const rest = workflow.tasks.map((task) => task.name).filter((name) => linked.has(name) && !seen.has(name));
    if (rest.length > 0) {
      chains.push(rest);
    }
  }

  return chains.filter((chain) => chain.length >= 2);
}

/** Рамки стадий вокруг потоково-связанных узлов: та же карточка, что в Observe. */
export function editorStageNodes(workflow: EditableWorkflow, nodes: EditorTaskFlowNode[]): EditorStageFlowNode[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const stages: EditorStageFlowNode[] = [];

  streamChains(workflow).forEach((chain, index) => {
    const members = chain.map((name) => byId.get(name)).filter((node): node is EditorTaskFlowNode => node !== undefined);
    if (members.length < 2) {
      return;
    }

    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of members) {
      minX = Math.min(minX, node.position.x);
      minY = Math.min(minY, node.position.y);
      maxX = Math.max(maxX, node.position.x + (node.width ?? EDITOR_NODE_WIDTH));
      maxY = Math.max(maxY, node.position.y + (node.height ?? 0));
    }

    stages.push({
      id: `stage:${chain.join("+")}`,
      type: "editorStage",
      position: { x: minX - STAGE_PAD, y: minY - STAGE_HEAD },
      width: maxX - minX + STAGE_PAD * 2,
      height: maxY - minY + STAGE_HEAD + STAGE_PAD,
      draggable: false,
      selectable: false,
      zIndex: -1,
      data: { title: chain.join(" → "), color: phaseColor(index) },
    });
  });

  return stages;
}

/** Ребро из жеста соединения; null — такая пара портов ребром не бывает. */
export function edgeOfConnection(connection: Connection): EditableEdge | null {
  const src = portOfHandle(connection.source, connection.sourceHandle);
  const dst = portOfHandle(connection.target, connection.targetHandle);
  if (src === null || dst === null) {
    return null;
  }

  if (edgeKindOf(src.kind, dst.kind) === null) {
    return null;
  }

  if (src.task === dst.task) {
    return null;
  }

  return { src, dst };
}
