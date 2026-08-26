import type { Node, NodeProps } from "@xyflow/react";
import type { ReactElement } from "react";

export type StageNodeData = {
  title: string;
  done: number;
  total: number;
  color: string;
};

export type StageFlowNode = Node<StageNodeData, "stage">;

/** Группа стадии: карточка в цвете фазы с заголовком и счётчиком завершённых задач. */
export function StageNode({ data }: NodeProps<StageFlowNode>): ReactElement {
  return (
    <div className="stage-node" style={{ "--phase-color": data.color } as React.CSSProperties}>
      <div className="stage-node__title">
        <span className="tag">stage</span>
        {data.title}
        <span className="stage-node__count">
          {data.done}/{data.total}
        </span>
      </div>
    </div>
  );
}
