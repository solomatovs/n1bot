import {
  Background,
  ReactFlow,
  useReactFlow,
  type EdgeTypes,
  type NodeTypes,
  type Node,
  type Edge,
} from "@xyflow/react";
import { useEffect, useMemo, useState, type ReactElement } from "react";

import "@xyflow/react/dist/style.css";

import type { Catalog, NodePosition } from "../../model/catalog";
import { buildGraph, laneNodes, type DatasetNode as DatasetFlowNode, type FlowEdge as FlowEdgeType, type GraphOptions } from "../../model/graph";
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

type Laid = {
  key: string;
  nodes: DatasetFlowNode[];
  edges: FlowEdgeType[];
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

/** Холст диаграммы: узлы наборов в дорожках слоёв, рёбра потоков, подсветка
 * цепочки при наведении и выборе. */
export function Canvas({ catalog, options, saved, activeId, onActivate, tidyCount }: Props): ReactElement {
  const { fitView } = useReactFlow();
  const [hoverId, setHoverId] = useState<string | undefined>(undefined);
  const [laid, setLaid] = useState<Laid | null>(null);
  const key = layoutKey(catalog, options, tidyCount);

  useEffect(() => {
    let cancelled = false;
    const built = buildGraph(catalog, options);
    const positions = tidyCount > 0 ? [] : saved;
    void computeLayout({
      nodes: built.nodes,
      edges: built.edges,
      partitionOf: (node) => catalog.layerIndex(node.data.dataset.layer_id),
      saved: positions,
    }).then((nodes) => {
      if (cancelled) {
        return;
      }

      setLaid({ key, nodes, edges: built.edges });
      window.setTimeout(() => {
        void fitView({ padding: 0.15, maxZoom: 1 });
      }, 0);
    });

    return () => {
      cancelled = true;
    };
  }, [catalog, options, saved, key, tidyCount, fitView]);

  const flow = useMemo(() => {
    if (laid === null) {
      return { nodes: [] as Node[], edges: [] as Edge[] };
    }

    const lit = highlight(laid.nodes, laid.edges, { activeId, hoverId });
    const lanes = laneNodes(catalog, lit.nodes, options.showDiff);
    return { nodes: [...lanes, ...lit.nodes] as Node[], edges: lit.edges as Edge[] };
  }, [laid, activeId, hoverId, catalog, options.showDiff]);

  return (
    <div className="canvas" data-testid="canvas" data-ready={laid !== null}>
      <ArrowMarkers />
      <ReactFlow
        nodes={flow.nodes}
        edges={flow.edges}
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
