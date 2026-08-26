import type { Node, NodeProps } from "@xyflow/react";
import type { ReactElement } from "react";

export type StageNodeData = {
  title: string;
  done: number;
  total: number;
};

export type StageFlowNode = Node<StageNodeData, "stage">;

/** Группа стадии: заголовок и счётчик завершённых задач; размер задаёт раскладка. */
export function StageNode({ data }: NodeProps<StageFlowNode>): ReactElement {
  return (
    <div className="stage-node">
      <div className="stage-node__title">
        {data.title}
        <span className="faint">
          {" "}
          {data.done}/{data.total}
        </span>
      </div>
    </div>
  );
}
