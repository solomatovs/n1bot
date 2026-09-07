import {
  Background,
  ReactFlow,
  applyEdgeChanges,
  applyNodeChanges,
  useNodesInitialized,
  useReactFlow,
  type Edge,
  type EdgeChange,
  type EdgeTypes,
  type Node,
  type NodeChange,
  type NodeTypes,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type ReactElement } from "react";

import "@xyflow/react/dist/style.css";

import type { Catalog, Position } from "../../model/catalog";
import type { EditActions, LinkRemoval, NodeMove } from "../../model/editing";
import {
  NODE_HANDLE,
  buildGraph,
  centerOf,
  freePosition,
  groupAt,
  groupFrames,
  groupOfFrame,
  measuredOf,
  unplaced,
  type FlowEdge as FlowEdgeType,
  type FrameActions,
  type FrameLayout,
  type GraphOptions,
  type ProcessFlowNode,
} from "../../model/graph";
import { highlight } from "../../model/highlight";
import { computeLayout } from "../../model/layout";
import { OBJECT_DRAG_TYPE, ObjectParam } from "../../model/refParam";
import { ArrowMarkers, FlowEdge } from "./FlowEdge";
import { GroupFrame } from "./GroupFrame";
import { ProcessNode } from "./ProcessNode";
import "./canvas.css";

const NODE_TYPES: NodeTypes = { process: ProcessNode, group: GroupFrame };
/** Радиус, в котором линия притягивается к ближайшей ручке: целиться в
 * шестипиксельную точку не нужно. */
const CONNECTION_RADIUS = 48;
/** Зазор между карточками, которые холст ставит столбиком сам. */
const FREE_ROW_GAP = 40;
const EDGE_TYPES: EdgeTypes = { flow: FlowEdge };

type Props = {
  catalog: Catalog;
  options: GraphOptions;
  activeId: string | undefined;
  onActivate: (nodeId: string | undefined) => void;
  /** Счётчик «прибрать»: каждое изменение заново раскладывает все узлы ELK;
   * на открытом черновике новые позиции уходят в процесс, на опубликованном
   * «прибрать» меняет только вид и черновика не заводит. */
  tidyCount: number;
  persistTidy: boolean;
  /** Правки черновика: карточки двигаются, тянутся за края и тащатся в
   * рамки, объекты из дерева падают на холст, линия от колонки к колонке добавляет пару,
   * выбранная линия снимается Delete, двойной клик по линии открывает
   * форму потока. Без правок холст только читается. */
  editing: EditActions | undefined;
  onFlowOpen: ((flowId: string) => void) | undefined;
};

/** Ключ раскладки: что меняет размеры или состав узлов, то и перекладывает граф. */
function layoutKey(catalog: Catalog, options: GraphOptions): string {
  return [
    catalog.nodes.map((node) => `${node.id}@${node.position?.x ?? "?"},${node.position?.y ?? "?"}`).join(";"),
    catalog.flows.length,
    catalog.groups.length,
    options.showMode,
    [...options.hidden].sort().join(","),
  ].join("|");
}

/** Холст процесса. Карточки стоят по позициям процесса; узлы без позиции
 * раскладывает ELK по замерам React Flow и холст запоминает их места до
 * первого сдвига. Рамки групп и подсветка считаются от разложенных узлов.
 * Карточка входит в группу, когда её отпускают над чужой рамкой; вытащенная
 * из рамки группу не теряет — снять её можно только в панели карточки.
 * Сдвиги линий и места пустых рамок живут только на странице. */
export function Canvas({
  catalog,
  options,
  activeId,
  onActivate,
  tidyCount,
  persistTidy,
  editing,
  onFlowOpen,
}: Props): ReactElement {
  const { fitView, screenToFlowPosition } = useReactFlow();
  const initialized = useNodesInitialized();
  const [hoverId, setHoverId] = useState<string | undefined>(undefined);
  const [nodes, setNodes] = useState<ProcessFlowNode[]>([]);
  const [edges, setEdges] = useState<FlowEdgeType[]>([]);
  const [laid, setLaid] = useState<string | null>(null);
  const [layouts, setLayouts] = useState(0);
  const [bends, setBends] = useState<ReadonlyMap<string, Position>>(new Map());
  const [emptyPositions, setEmptyPositions] = useState<ReadonlyMap<string, Position>>(new Map());
  const [dropTarget, setDropTarget] = useState<string | undefined>(undefined);
  const autoPositions = useRef(new Map<string, Position>());
  const tidied = useRef(tidyCount);
  const key = layoutKey(catalog, options);
  const signature = `${key}#${tidyCount}`;

  // проход 1: новый состав, позиции или режим — узлы по позициям; замеры
  // прежних карточек переносятся, чтобы рёбра не пропадали до нового замера
  useEffect(() => {
    const built = buildGraph(catalog, options, autoPositions.current);
    setNodes((current) => {
      const measured = new Map(current.map((node) => [node.id, node.measured]));
      return built.nodes.map((node) => {
        const size = measured.get(node.id);
        return size === undefined ? node : { ...node, measured: size };
      });
    });
    setEdges(built.edges);
    setLaid(null);
  }, [catalog, options, key]);

  // проход 2: узлы замерены — раскладка тех, у кого нет места, или всех по «tidy»;
  // пустой холст замерять нечего, он готов сразу (useNodesInitialized без узлов — false)
  useEffect(() => {
    if (laid === signature) {
      return;
    }

    // узлы ещё не построены после смены процесса — ждём; построены, но не
    // замерены — тоже; пустой процесс замерять нечего
    if (nodes.length !== catalog.nodes.length) {
      return;
    }

    if (!initialized && nodes.length > 0) {
      return;
    }

    const tidy = tidied.current !== tidyCount;
    const pending = unplaced(nodes, autoPositions.current);
    if (!tidy && pending.length === 0) {
      // все узлы на местах: холст готов без ELK; счётчик готовностей растёт,
      // первая готовность вписывает граф в окно
      setLaid(signature);
      setLayouts((count) => count + 1);
      if (layouts === 0) {
        window.setTimeout(() => {
          void fitView({ padding: 0.15, maxZoom: 1 });
        }, 0);
      }
      return;
    }

    // узлы без места рядом со стоящими: столбиком справа от крайней карточки,
    // чтобы не лечь поверх; ELK только по «tidy» или когда места нет ни у кого
    const placedAny = nodes.some((node) => !node.hidden && !pending.includes(node));
    if (!tidy && placedAny) {
      const moves: NodeMove[] = [];
      const positioned = nodes.map((node) => node);
      let anchor = freePosition(nodes.filter((node) => !pending.includes(node)));
      for (const node of pending) {
        const at = positioned.findIndex((item) => item.id === node.id);
        const position = { x: anchor.x, y: anchor.y };
        autoPositions.current.set(node.id, position);
        moves.push({ node: node.data.node, position, groupId: node.data.node.group_id });
        positioned[at] = { ...node, position };
        const size = measuredOf(node) ?? { width: 0, height: 0 };
        anchor = { x: anchor.x, y: anchor.y + size.height + FREE_ROW_GAP };
      }

      setNodes(positioned);
      setLaid(signature);
      setLayouts((count) => count + 1);
      return;
    }

    let cancelled = false;
    void computeLayout({ nodes, edges }).then((placed) => {
      if (cancelled) {
        return;
      }

      tidied.current = tidyCount;
      const chosen = tidy ? nodes.filter((node) => !node.hidden) : pending;
      const moves: NodeMove[] = [];
      const positioned = nodes.map((node) => {
        const position = placed.get(node.id);
        if (position === undefined || !chosen.some((item) => item.id === node.id)) {
          return node;
        }

        autoPositions.current.set(node.id, position);
        moves.push({ node: node.data.node, position, groupId: node.data.node.group_id });
        return { ...node, position };
      });

      setNodes(positioned);
      setLaid(signature);
      setLayouts((count) => count + 1);
      if (tidy && persistTidy && editing !== undefined) {
        editing.moveNodes(moves);
      }
      window.setTimeout(() => {
        void fitView({ padding: 0.15, maxZoom: 1 });
      }, 0);
    });

    return () => {
      cancelled = true;
    };
  }, [initialized, laid, signature, nodes, edges, catalog, tidyCount, persistTidy, editing, fitView, layouts]);

  // рамки не в состоянии узлов: сдвиг пустой рамки запоминается отдельно
  const onNodesChange = useCallback((changes: NodeChange<ProcessFlowNode>[]) => {
    const moved = new Map<string, Position>();
    for (const change of changes) {
      if (change.type !== "position" || change.position === undefined) {
        continue;
      }

      const groupId = groupOfFrame(change.id);
      if (groupId !== undefined) {
        moved.set(groupId, change.position);
      }
    }

    if (moved.size > 0) {
      setEmptyPositions((current) => new Map([...current, ...moved]));
    }

    setNodes((current) => applyNodeChanges(changes, current));
  }, []);

  const onBend = useCallback((edgeId: string, bend: Position) => {
    setBends((current) => new Map([...current, [edgeId, bend]]));
  }, []);

  // выбор линий живёт в состоянии рёбер; снятие линий уходит в черновик
  const onEdgesChange = useCallback((changes: EdgeChange<FlowEdgeType>[]) => {
    setEdges((current) => applyEdgeChanges(changes, current));
  }, []);

  const edgesDeleted = (deleted: Edge[]): void => {
    if (editing === undefined) {
      return;
    }

    const removed: LinkRemoval[] = [];
    for (const item of deleted) {
      const known = edges.find((edge) => edge.id === item.id);
      if (known?.data === undefined) {
        continue;
      }

      removed.push({ flowId: known.data.flow.id, pair: known.data.pair });
    }

    if (removed.length > 0) {
      editing.removeLinks(removed);
    }
  };

  const ready = laid === signature;

  const frameActions = useMemo<FrameActions | undefined>(() => {
    if (editing === undefined) {
      return undefined;
    }

    return { onRename: editing.renameGroup, onRemove: editing.removeGroup };
  }, [editing]);

  const frameLayout = useMemo<FrameLayout>(() => ({ emptyPositions, dropTarget }), [emptyPositions, dropTarget]);

  const flow = useMemo(() => {
    const lit = highlight(nodes, edges, { activeId, hoverId });
    const frames = ready ? groupFrames(catalog, lit.nodes, options.showDiff, frameActions, frameLayout) : [];
    const bent = lit.edges.map((edge) => {
      if (edge.data === undefined) {
        return edge;
      }

      return { ...edge, data: { ...edge.data, bend: bends.get(edge.id), onBend } };
    });
    const resizable = lit.nodes.map((node) => {
      if (editing === undefined) {
        return node;
      }

      const onResize = (position: Position, width: number): void => {
        editing.resizeNode({ node: node.data.node, position, width });
      };
      return { ...node, data: { ...node.data, onResize } };
    });
    return { frames, nodes: [...frames, ...resizable] as Node[], edges: bent };
  }, [
    nodes,
    edges,
    activeId,
    hoverId,
    catalog,
    options.showDiff,
    ready,
    frameActions,
    frameLayout,
    bends,
    onBend,
    editing,
  ]);

  const dropObject = (event: DragEvent<HTMLDivElement>): void => {
    setDropTarget(undefined);
    if (editing === undefined) {
      return;
    }

    const ref = ObjectParam.parse(event.dataTransfer.getData(OBJECT_DRAG_TYPE));
    if (ref === undefined) {
      return;
    }

    event.preventDefault();
    const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    editing.addNode(ref, point, groupAt(flow.frames, point.x, point.y));
  };

  /** Чужая рамка под центром перетаскиваемой карточки: рамки считаются без
   * самих перетаскиваемых карточек, иначе своя рамка тянулась бы за ними. */
  const frameUnder = (item: Node, dragged: Node[]): string | null => {
    const current = nodes.find((node) => node.id === item.id);
    if (current === undefined) {
      return null;
    }

    const draggedIds = new Set(dragged.map((node) => node.id));
    const frames = groupFrames(catalog, nodes, false, frameActions, frameLayout, draggedIds);
    const center = centerOf({ ...current, position: item.position });
    return groupAt(frames, center.x, center.y, current.data.node.group_id);
  };

  const dragMove = (item: Node, dragged: Node[]): void => {
    if (editing === undefined || item.type !== "process") {
      return;
    }

    setDropTarget(frameUnder(item, dragged) ?? undefined);
  };

  const dragStop = (dragged: Node[]): void => {
    setDropTarget(undefined);
    if (editing === undefined) {
      return;
    }

    const moves: NodeMove[] = [];
    for (const item of dragged) {
      const current = nodes.find((node) => node.id === item.id);
      if (current === undefined) {
        continue;
      }

      const groupId = frameUnder(item, dragged) ?? current.data.node.group_id;
      autoPositions.current.delete(item.id);
      moves.push({ node: current.data.node, position: item.position, groupId });
    }

    if (moves.length > 0) {
      editing.moveNodes(moves);
    }
  };

  return (
    <div
      className="canvas"
      data-testid="canvas"
      data-ready={ready}
      data-layouts={layouts}
      data-drop={dropTarget !== undefined}
    >
      {/* data-layouts — сколько раз холст становился готовым: тесты ждут следующую готовность */}
      <ArrowMarkers />
      <ReactFlow
        onDragOver={(event) => {
          if (editing === undefined || !event.dataTransfer.types.includes(OBJECT_DRAG_TYPE)) {
            return;
          }

          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
          const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
          setDropTarget(groupAt(flow.frames, point.x, point.y) ?? undefined);
        }}
        onDragLeave={() => {
          setDropTarget(undefined);
        }}
        onDrop={dropObject}
        nodes={flow.nodes}
        edges={flow.edges}
        onNodesChange={onNodesChange as (changes: NodeChange[]) => void}
        onEdgesChange={onEdgesChange}
        onEdgesDelete={edgesDeleted}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        nodesConnectable={editing !== undefined}
        connectionRadius={CONNECTION_RADIUS}
        nodesDraggable={editing !== undefined}
        elementsSelectable
        edgesFocusable={editing !== undefined}
        deleteKeyCode={editing === undefined ? null : ["Backspace", "Delete"]}
        selectionKeyCode="Shift"
        multiSelectionKeyCode={["Meta", "Control"]}
        minZoom={0.1}
        onNodeDrag={(_event, node, dragged) => {
          dragMove(node, dragged);
        }}
        onNodeDragStop={(_event, _node, dragged) => {
          dragStop(dragged);
        }}
        onConnect={(connection) => {
          if (editing === undefined || connection.source === connection.target) {
            return;
          }

          editing.connect({
            from: connection.source,
            to: connection.target,
            fromColumn: columnOfHandle(connection.sourceHandle),
            toColumn: columnOfHandle(connection.targetHandle),
          });
        }}
        onEdgeDoubleClick={(_event, edge) => {
          const known = edges.find((item) => item.id === edge.id);
          if (known?.data !== undefined) {
            onFlowOpen?.(known.data.flow.id);
          }
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

/** Колонка по ручке: ручка карточки целиком колонки не называет. */
function columnOfHandle(handle: string | null | undefined): string | undefined {
  if (handle === null || handle === undefined || handle === NODE_HANDLE) {
    return undefined;
  }

  return handle;
}
