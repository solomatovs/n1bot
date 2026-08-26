import { MarkerType, Position, type Connection, type Edge as FlowEdge, type NodeHandle } from "@xyflow/react";

import type { TaskPositions } from "../../model/layout";
import { edgeId, edgeKindOf, type EditableEdge, type EditableTask, type EditableWorkflow } from "../../model/spec";
import type { ToolCatalog } from "../../model/workflow";
import { sideHandle } from "../graph/geometry";
import { editorNodeHeight, editorPorts, type EditorTaskData, type EditorTaskFlowNode } from "./EditorTaskNode";
import { portOfHandle } from "./handles";

/** Модель редактора → узлы и рёбра React Flow и обратно из жестов пользователя. */

export const EDITOR_NODE_WIDTH = 240;

const EDGE_COLOR = {
  control: "var(--edge-control)",
  value: "var(--edge-value)",
  stream: "var(--edge-stream)",
} as const;

export function taskData(task: EditableTask, catalog: ToolCatalog, selected: string | null, issue: string): EditorTaskData {
  const facts = catalog[task.tool];
  const argNames = new Set<string>();
  for (const arg of facts?.args ?? []) {
    argNames.add(arg.name);
  }
  for (const name of Object.keys(task.args)) {
    argNames.add(name);
  }

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
    argNames: [...argNames],
    args: task.args,
    readPorts,
    writePorts,
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
    const data = taskData(task, catalog, selected, issues.get(task.name) ?? "");
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
  const { inputs, outputs } = editorPorts(data);
  const handles: NodeHandle[] = [];
  for (const port of inputs) {
    handles.push(sideHandle("target", Position.Left, 0, port.top, port.id));
  }
  for (const port of outputs) {
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
