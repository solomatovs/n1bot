import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { ReactElement } from "react";

import type { TaskStatus } from "../../model/status";

export type TaskNodeData = {
  task: string;
  tool: string;
  status: TaskStatus;
  duration: string;
  selected: boolean;
};

export type TaskFlowNode = Node<TaskNodeData, "task">;

/** Узел задачи: имя, инструмент, статус кольцом, длительность. */
export function TaskNode({ data }: NodeProps<TaskFlowNode>): ReactElement {
  return (
    <div className="task-node" data-status={data.status} data-selected={data.selected}>
      <Handle type="target" position={Position.Left} />
      <div className="task-node__ring" />
      <div className="task-node__body">
        <div className="task-node__name">{data.task}</div>
        <div className="task-node__meta mono">
          {data.tool} · {data.status}
          {data.duration !== "—" ? ` · ${data.duration}` : ""}
        </div>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
