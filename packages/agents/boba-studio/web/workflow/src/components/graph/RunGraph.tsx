import { Background, Controls, MiniMap, ReactFlow, type NodeTypes } from "@xyflow/react";
import { useMemo, type ReactElement } from "react";

import "@xyflow/react/dist/style.css";

import type { RunState } from "../../model/workflow";
import { flowOf } from "./flow";
import { StageNode } from "./StageNode";
import { TaskNode } from "./TaskNode";

const NODE_TYPES: NodeTypes = { task: TaskNode, stage: StageNode };

type Props = {
  run: RunState;
  selectedTask: string | null;
  onSelectTask: (task: string | null) => void;
};

/** Граф запуска: узлы задач в группах стадий, живые статусы из состояния. */
export function RunGraph({ run, selectedTask, onSelectTask }: Props): ReactElement {
  const flow = useMemo(() => flowOf(run, selectedTask), [run, selectedTask]);

  return (
    <ReactFlow
      nodes={flow.nodes}
      edges={flow.edges}
      nodeTypes={NODE_TYPES}
      fitView
      fitViewOptions={{ maxZoom: 1 }}
      nodesConnectable={false}
      elementsSelectable
      onNodeClick={(_event, node) => {
        if (node.type === "task") {
          onSelectTask(node.id);
        }
      }}
      onPaneClick={() => {
        onSelectTask(null);
      }}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable />
    </ReactFlow>
  );
}
