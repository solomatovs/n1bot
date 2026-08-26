import { MarkerType, type Edge as FlowEdge, type Node as FlowNode } from "@xyflow/react";

import { layoutGraph, type LaidEdge } from "../../model/layout";
import { phaseColor } from "../../model/summary";
import { formatDuration } from "../../model/time";
import type { RunState, TaskState } from "../../model/workflow";
import type { StageFlowNode } from "./StageNode";
import type { TaskFlowNode } from "./TaskNode";

/** Перевод состояния запуска в узлы и рёбра React Flow; раскладка — из model/layout. */

export type RunFlow = {
  nodes: FlowNode[];
  edges: FlowEdge[];
};

const EDGE_STYLE: Record<LaidEdge["kind"], { color: string; dash: string; animated: boolean }> = {
  control: { color: "var(--edge-control)", dash: "", animated: false },
  value: { color: "var(--edge-value)", dash: "6 4", animated: false },
  stream: { color: "var(--edge-stream)", dash: "4 4", animated: true },
};

function stageTitle(stageId: string): string {
  return stageId.replace(/^stage:/, "");
}

function finished(state: TaskState | undefined): boolean {
  if (state === undefined) {
    return false;
  }

  return state.status !== "pending" && state.status !== "running";
}

export function flowOf(run: RunState, selectedTask: string | null): RunFlow {
  const layout = layoutGraph(run.graph);

  const stageNodes: StageFlowNode[] = layout.stages.map((box) => {
    const index = run.graph.stages.findIndex((candidate) => candidate.id === box.stage);
    const stage = run.graph.stages[index];
    const tasks = stage?.tasks ?? [];
    return {
      id: box.stage,
      type: "stage",
      position: box.position,
      style: { width: box.size.width, height: box.size.height },
      draggable: false,
      selectable: false,
      data: {
        title: stageTitle(box.stage),
        done: tasks.filter((task) => finished(run.tasks[task])).length,
        total: tasks.length,
        color: phaseColor(Math.max(index, 0)),
      },
    };
  });

  const taskNodes: TaskFlowNode[] = layout.tasks.map((box) => {
    const state = run.tasks[box.task];
    const spec = run.graph.spec.tasks[box.task];
    return {
      id: box.task,
      type: "task",
      parentId: box.stage,
      extent: "parent",
      position: box.position,
      style: { width: box.size.width, height: box.size.height },
      data: {
        task: box.task,
        tool: spec?.tool ?? "?",
        status: state?.status ?? "pending",
        duration: state === undefined ? "—" : formatDuration(state.started_at, state.finished_at),
        selected: box.task === selectedTask,
      },
    };
  });

  const edges: FlowEdge[] = layout.edges.map((edge) => {
    const style = EDGE_STYLE[edge.kind];
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.label === "" ? undefined : edge.label,
      animated: style.animated,
      style: { stroke: style.color, strokeDasharray: style.dash },
      markerEnd: { type: MarkerType.ArrowClosed, color: style.color },
      zIndex: 1,
    };
  });

  return { nodes: [...stageNodes, ...taskNodes], edges };
}
