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
import { useCallback, useEffect, useMemo, useState, type DragEvent, type ReactElement } from "react";

import "@xyflow/react/dist/style.css";

import type { Catalog, NodePosition, ObjectRef } from "../../model/catalog";
import {
  buildGraph,
  laneNodes,
  layerOfLane,
  measuredOf,
  type FlowEdge as FlowEdgeType,
  type GraphOptions,
  type LayerNode,
  type ProcessFlowNode,
} from "../../model/graph";
import { highlight } from "../../model/highlight";
import { computeLayout } from "../../model/layout";
import { OBJECT_DRAG_TYPE, ObjectParam } from "../../model/refParam";
import { ArrowMarkers, FlowEdge } from "./FlowEdge";
import { LayerLane } from "./LayerLane";
import { ProcessNode } from "./ProcessNode";

const NODE_TYPES: NodeTypes = { process: ProcessNode, layer: LayerLane };
const EDGE_TYPES: EdgeTypes = { flow: FlowEdge };

type Props = {
  catalog: Catalog;
  options: GraphOptions;
  saved: NodePosition[];
  activeId: string | undefined;
  onActivate: (nodeId: string | undefined) => void;
  /** Счётчик «прибрать»: каждое изменение заново раскладывает узлы ELK. */
  tidyCount: number;
  /** Правки черновика: соединение handle'ов заводит поток, клик по ребру открывает его. */
  onConnect: ((from: string, to: string) => void) | undefined;
  onFlowClick: ((flowId: string) => void) | undefined;
  /** Правки черновика: объект из дерева, брошенный на дорожку слоя, становится
   * узлом этого слоя; мимо дорожек — слой спросит страница. */
  onDrop: ((ref: ObjectRef, layerId: string | undefined) => void) | undefined;
  /** Владелец вида: узлы перетаскиваются, после перетаскивания или «прибрать»
   * наверх уходят позиции всех разложенных узлов для сохранения раскладки. */
  onMoved: ((positions: NodePosition[]) => void) | undefined;
};

function positionsOf(nodes: ProcessFlowNode[]): NodePosition[] {
  const positions: NodePosition[] = [];
  for (const node of nodes) {
    if (node.hidden === true) {
      continue;
    }

    positions.push({ node_id: node.id, x: node.position.x, y: node.position.y });
  }

  return positions;
}

/** Ключ раскладки: что меняет размеры или состав узлов, то и перекладывает граф. */
function layoutKey(catalog: Catalog, options: GraphOptions, tidyCount: number): string {
  return [
    catalog.nodes.length,
    catalog.flows.length,
    catalog.layers.length,
    options.showMode,
    [...options.nodeIds].sort().join(","),
    [...options.layerIds].sort().join(","),
    [...options.hidden].sort().join(","),
    tidyCount,
  ].join("|");
}

/** Подпись замеров видимых узлов: меняется, когда React Flow отдал новый размер. */
function sizesOf(nodes: ProcessFlowNode[]): string {
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
export function Canvas({
  catalog,
  options,
  saved,
  activeId,
  onActivate,
  tidyCount,
  onConnect,
  onFlowClick,
  onDrop,
  onMoved,
}: Props): ReactElement {
  const { fitView, screenToFlowPosition } = useReactFlow();
  const initialized = useNodesInitialized();
  const [hoverId, setHoverId] = useState<string | undefined>(undefined);
  const [nodes, setNodes] = useState<ProcessFlowNode[]>([]);
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
      partitionOf: (node) => catalog.layerIndex(node.data.node.layer_id),
      saved: positions,
    }).then((positioned) => {
      if (cancelled) {
        return;
      }

      setNodes(positioned);
      setLaid(signature);
      setLayouts((count) => count + 1);
      if (tidyCount > 0 && onMoved !== undefined) {
        onMoved(positionsOf(positioned));
      }
      window.setTimeout(() => {
        void fitView({ padding: 0.15, maxZoom: 1 });
      }, 0);
    });

    return () => {
      cancelled = true;
    };
  }, [initialized, laid, signature, nodes, edges, saved, tidyCount, catalog, fitView, onMoved]);

  const onNodesChange = useCallback((changes: NodeChange<ProcessFlowNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current));
  }, []);

  const ready = laid === signature;

  const flow = useMemo(() => {
    const lit = highlight(nodes, edges, { activeId, hoverId });
    const lanes = ready ? laneNodes(catalog, lit.nodes, options.showDiff, onDrop !== undefined) : [];
    return { lanes, nodes: [...lanes, ...lit.nodes] as Node[], edges: lit.edges };
  }, [nodes, edges, activeId, hoverId, catalog, options.showDiff, ready, onDrop]);

  const dropObject = (event: DragEvent<HTMLDivElement>): void => {
    if (onDrop === undefined) {
      return;
    }

    const ref = ObjectParam.parse(event.dataTransfer.getData(OBJECT_DRAG_TYPE));
    if (ref === undefined) {
      return;
    }

    event.preventDefault();
    const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    onDrop(ref, laneAt(flow.lanes, point.x, point.y));
  };

  return (
    <div className="canvas" data-testid="canvas" data-ready={ready} data-layouts={layouts}>
      <ArrowMarkers />
      <ReactFlow
        onDragOver={(event) => {
          if (onDrop !== undefined && event.dataTransfer.types.includes(OBJECT_DRAG_TYPE)) {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
          }
        }}
        onDrop={dropObject}
        nodes={flow.nodes}
        edges={flow.edges}
        onNodesChange={onNodesChange as (changes: NodeChange[]) => void}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        nodesConnectable={onConnect !== undefined}
        nodesDraggable={onMoved !== undefined}
        onNodeDragStop={(_event, moved) => {
          const current = nodes.map((node) => (node.id === moved.id ? { ...node, position: moved.position } : node));
          onMoved?.(positionsOf(current));
        }}
        elementsSelectable
        minZoom={0.1}
        onConnect={(connection) => {
          if (onConnect !== undefined && connection.source !== connection.target) {
            onConnect(connection.source, connection.target);
          }
        }}
        onEdgeClick={(_event, edge) => {
          onFlowClick?.(edge.id);
        }}
        onNodeClick={(_event, node) => {
          if (node.type === "process") {
            onActivate(node.id);
          }
        }}
        onNodeMouseEnter={(_event, node) => {
          if (node.type === "process") {
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

/** Слой дорожки, в которую попала точка холста; мимо дорожек — undefined. */
function laneAt(lanes: LayerNode[], x: number, y: number): string | undefined {
  for (const lane of lanes) {
    const width = lane.width ?? 0;
    const height = lane.height ?? 0;
    const inside = x >= lane.position.x && x <= lane.position.x + width && y >= lane.position.y && y <= lane.position.y + height;
    if (inside) {
      return layerOfLane(lane.id);
    }
  }

  return undefined;
}
