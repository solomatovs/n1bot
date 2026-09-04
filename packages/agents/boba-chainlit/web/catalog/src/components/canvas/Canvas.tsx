import {
  Background,
  ReactFlow,
  applyNodeChanges,
  useNodesInitialized,
  useReactFlow,
  type EdgeTypes,
  type Node,
  type NodeChange,
  type NodeTypes,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";

import "@xyflow/react/dist/style.css";

import type { Catalog, NodePosition } from "../../model/catalog";
import {
  buildGraph,
  laneNodes,
  measuredOf,
  type DatasetNode as DatasetFlowNode,
  type FlowEdge as FlowEdgeType,
  type GraphOptions,
} from "../../model/graph";
import { highlight } from "../../model/highlight";
import { computeLayout } from "../../model/layout";
import { DatasetNode } from "./DatasetNode";
import { ArrowMarkers, FlowEdge } from "./FlowEdge";
import { LayerLane } from "./LayerLane";

const NODE_TYPES: NodeTypes = { dataset: DatasetNode, layer: LayerLane };
const EDGE_TYPES: EdgeTypes = { flow: FlowEdge };

type Props = {
  catalog: Catalog;
  options: GraphOptions;
  saved: NodePosition[];
  activeId: string | undefined;
  onActivate: (datasetId: string | undefined) => void;
  /** Счётчик «прибрать»: каждое изменение заново раскладывает узлы ELK. */
  tidyCount: number;
};

/** Ключ раскладки: что меняет размеры или состав узлов, то и перекладывает граф. */
function layoutKey(catalog: Catalog, options: GraphOptions, tidyCount: number): string {
  return [
    catalog.datasets.length,
    catalog.flows.length,
    options.showMode,
    [...options.datasetIds].sort().join(","),
    [...options.layerIds].sort().join(","),
    [...options.hidden].sort().join(","),
    tidyCount,
  ].join("|");
}

/** Подпись замеров видимых узлов: меняется, когда React Flow отдал новый размер. */
function sizesOf(nodes: DatasetFlowNode[]): string {
  return nodes
    .filter((node) => !node.hidden)
    .map((node) => {
      const size = measuredOf(node);
      return size === undefined ? `${node.id}:?` : `${node.id}:${size.width}x${size.height}`;
    })
    .join(",");
}

/** Холст диаграммы в два прохода, как в liam: узлы рендерятся невидимыми и
 * React Flow их замеряет, затем ELK раскладывает по реальным размерам и холст
 * показывается. Дорожки слоёв и подсветка считаются от разложенных узлов. */
export function Canvas({ catalog, options, saved, activeId, onActivate, tidyCount }: Props): ReactElement {
  const { fitView } = useReactFlow();
  const initialized = useNodesInitialized();
  const [hoverId, setHoverId] = useState<string | undefined>(undefined);
  const [nodes, setNodes] = useState<DatasetFlowNode[]>([]);
  const [edges, setEdges] = useState<FlowEdgeType[]>([]);
  const [laid, setLaid] = useState<string | null>(null);
  const [layouts, setLayouts] = useState(0);
  const key = layoutKey(catalog, options, tidyCount);
  // размеры узлов входят в подпись: React Flow может замерить карточку позже,
  // чем отдал прежний размер, и тогда граф перекладывается по новому замеру
  const signature = `${key}#${sizesOf(nodes)}`;

  // проход 1: новый состав или режим — узлы в нуле, ждём замера
  useEffect(() => {
    const built = buildGraph(catalog, options);
    setNodes(built.nodes);
    setEdges(built.edges);
    setLaid(null);
  }, [catalog, options, key]);

  // проход 2: все видимые узлы замерены — раскладка по их размерам
  useEffect(() => {
    if (!initialized || laid === signature) {
      return;
    }

    let cancelled = false;
    const positions = tidyCount > 0 ? [] : saved;
    void computeLayout({
      nodes,
      edges,
      partitionOf: (node) => catalog.layerIndex(node.data.dataset.layer_id),
      saved: positions,
    }).then((positioned) => {
      if (cancelled) {
        return;
      }

      setNodes(positioned);
      setLaid(signature);
      setLayouts((count) => count + 1);
      window.setTimeout(() => {
        void fitView({ padding: 0.15, maxZoom: 1 });
      }, 0);
    });

    return () => {
      cancelled = true;
    };
  }, [initialized, laid, signature, nodes, edges, saved, tidyCount, catalog, fitView]);

  const onNodesChange = useCallback((changes: NodeChange<DatasetFlowNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current));
  }, []);

  const ready = laid === signature;

  const flow = useMemo(() => {
    const lit = highlight(nodes, edges, { activeId, hoverId });
    const lanes = ready ? laneNodes(catalog, lit.nodes, options.showDiff) : [];
    return { nodes: [...lanes, ...lit.nodes] as Node[], edges: lit.edges };
  }, [nodes, edges, activeId, hoverId, catalog, options.showDiff, ready]);

  return (
    <div className="canvas" data-testid="canvas" data-ready={ready} data-layouts={layouts}>
      <ArrowMarkers />
      <ReactFlow
        nodes={flow.nodes}
        edges={flow.edges}
        onNodesChange={onNodesChange as (changes: NodeChange[]) => void}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        nodesConnectable={false}
        nodesDraggable={false}
        elementsSelectable
        minZoom={0.1}
        onNodeClick={(_event, node) => {
          if (node.type === "dataset") {
            onActivate(node.id);
          }
        }}
        onNodeMouseEnter={(_event, node) => {
          if (node.type === "dataset") {
            setHoverId(node.id);
          }
        }}
        onNodeMouseLeave={() => {
          setHoverId(undefined);
        }}
        onPaneClick={() => {
          onActivate(undefined);
        }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} />
      </ReactFlow>
    </div>
  );
}
