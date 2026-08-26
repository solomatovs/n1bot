import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { ReactElement } from "react";

import type { TaskStatus } from "../../model/status";

export type TaskNodeData = {
  task: string;
  tool: string;
  status: TaskStatus;
  duration: string;
  /** Сводка итога: вид и цифра; пусто — итога ещё нет. */
  result: string;
  selected: boolean;
};

export type TaskFlowNode = Node<TaskNodeData, "task">;

const MARK: Record<TaskStatus, string> = {
  pending: "",
  running: "…",
  done: "✓",
  failed: "✕",
  skipped: "–",
  stopped: "■",
};

/** Узел задачи: кольцо статуса, имя, инструмент и длительность. */
export function TaskNode({ data }: NodeProps<TaskFlowNode>): ReactElement {
  return (
    <div className="task-node" data-status={data.status} data-selected={data.selected}>
      <Handle type="target" position={Position.Left} />
      <div className="task-node__ring">{MARK[data.status]}</div>
      <div className="task-node__body">
        <div className="task-node__name">{data.task}</div>
        <div className="task-node__meta">
          {data.tool} · {data.status}
          {data.duration !== "—" ? ` · ${data.duration}` : ""}
        </div>
        {data.result !== "" && <div className="task-node__result">{data.result}</div>}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
