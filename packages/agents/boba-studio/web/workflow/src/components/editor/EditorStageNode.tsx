import type { Node, NodeProps } from "@xyflow/react";
import type { CSSProperties, ReactElement } from "react";

export type EditorStageData = {
  title: string;
  color: string;
};

export type EditorStageFlowNode = Node<EditorStageData, "editorStage">;

/** Рамка будущей стадии в редакторе: потоково-связанные задачи исполняются
 * вместе, и билд показывает их той же карточкой стадии, что Observe. */
export function EditorStageNode({ data }: NodeProps<EditorStageFlowNode>): ReactElement {
  return (
    <div className="stage-node" style={{ "--phase-color": data.color } as CSSProperties}>
      <div className="stage-node__title">
        <span className="tag">stage</span>
        {data.title}
      </div>
    </div>
  );
}
