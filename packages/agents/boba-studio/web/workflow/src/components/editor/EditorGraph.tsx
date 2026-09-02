import {
  Background,
  Controls,
  ReactFlow,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type NodeTypes,
} from "@xyflow/react";
import { useCallback, useMemo, type ReactElement } from "react";

import type { TaskPositions } from "../../model/layout";
import type { EditableEdge, EditableWorkflow } from "../../model/spec";
import type { ToolCatalog } from "../../model/workflow";
import { EditorStageNode, type EditorStageFlowNode } from "./EditorStageNode";
import { EditorTaskNode, type EditorTaskFlowNode } from "./EditorTaskNode";
import { edgeOfConnection, editorEdges, editorNodes, editorStageNodes } from "./flow";

const NODE_TYPES: NodeTypes = { editorTask: EditorTaskNode, editorStage: EditorStageNode };

type EditorFlowNode = EditorTaskFlowNode | EditorStageFlowNode;

type Props = {
  workflow: EditableWorkflow;
  positions: TaskPositions;
  catalog: ToolCatalog;
  selected: string | null;
  issues: Map<string, string>;
  onMove: (task: string, x: number, y: number) => void;
  onSelect: (task: string | null) => void;
  onConnect: (edge: EditableEdge) => void;
  onRemoveEdge: (id: string) => void;
  onRemoveTask: (task: string) => void;
  onBadConnection: () => void;
};

/** Канвас редактора: модель — источник истины, жесты переводятся в правки модели. */
export function EditorGraph(props: Props): ReactElement {
  const { workflow, positions, catalog, selected, issues } = props;
  const nodes = useMemo<EditorFlowNode[]>(() => {
    const tasks = editorNodes(workflow, positions, catalog, selected, issues);
    return [...editorStageNodes(workflow, tasks), ...tasks];
  }, [workflow, positions, catalog, selected, issues]);
  const edges = useMemo(() => editorEdges(workflow), [workflow]);

  const onNodesChange = useCallback(
    (changes: NodeChange<EditorFlowNode>[]) => {
      for (const change of changes) {
        if ("id" in change && change.id.startsWith("stage:")) {
          continue;
        }

        if (change.type === "position" && change.position !== undefined) {
          props.onMove(change.id, change.position.x, change.position.y);
        }

        if (change.type === "remove") {
          props.onRemoveTask(change.id);
        }
      }
    },
    [props],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      for (const change of changes) {
        if (change.type === "remove") {
          props.onRemoveEdge(change.id);
        }
      }
    },
    [props],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      const edge = edgeOfConnection(connection);
      if (edge === null) {
        props.onBadConnection();
        return;
      }

      props.onConnect(edge);
    },
    [props],
  );

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onNodeClick={(_event, node) => {
        props.onSelect(node.id);
      }}
      onPaneClick={() => {
        props.onSelect(null);
      }}
      fitView
      fitViewOptions={{ maxZoom: 1 }}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
