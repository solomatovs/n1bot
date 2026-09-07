import { BaseEdge, EdgeLabelRenderer, getBezierPath, useReactFlow, type EdgeProps } from "@xyflow/react";
import { useRef, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactElement } from "react";

import type { Position } from "../../model/catalog";
import type { FlowEdge as FlowEdgeType } from "../../model/graph";

export const ARROW_MARKER = "catalog-arrow";
export const ARROW_MARKER_HIGHLIGHTED = "catalog-arrow-highlighted";
export const ARROW_MARKER_SELECTED = "catalog-arrow-selected";

const PARTICLES = [0, 0.55, 1.1];
/** Ширина невидимой полосы вдоль линии, за которую её таскают. */
const GRIP_WIDTH = 16;
/** Сдвиг, начиная с которого нажатие считается перетаскиванием, а не кликом. */
const DRAG_THRESHOLD = 3;
/** Наименьший вынос управляющих точек кривой: линия у ручки выходит горизонтально. */
const MIN_CONTROL = 24;

type Drag = { pointerId: number; origin: Position; base: Position; moved: boolean };

/** Линия потока между колонками (или карточками): стрелка на конце, ярлык
 * потока на первой линии, при подсветке бегущие частицы. Линию можно
 * оттащить за середину, чтобы открыть то, что под ней; выбранная линия
 * отмечена своим цветом — её снимает Delete. Перенос RelationshipEdge из
 * liam erd-core. */
export function FlowEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
  data,
}: EdgeProps<FlowEdgeType>): ReactElement | null {
  const { screenToFlowPosition } = useReactFlow();
  const drag = useRef<Drag | null>(null);
  // клик после перетаскивания не должен выбирать линию
  const dragged = useRef(false);
  if (data === undefined) {
    return null;
  }

  const bend = data.bend;
  const [path, labelX, labelY] = bentPath(
    { sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition },
    bend,
  );
  const status = data.showDiff ? data.status : "unchanged";
  const marker = markerOf(data.isHighlighted, selected === true);
  const pairTitle = data.pair === undefined ? "" : `${data.pair.from_column} → ${data.pair.to_column}`;
  const showLabel = data.label !== "";
  const classes = ["flow-edge"];
  if (data.isHighlighted) {
    classes.push("flow-edge--lit");
  }

  const dragStart = (event: ReactPointerEvent): void => {
    if (data.onBend === undefined || event.button !== 0) {
      return;
    }

    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragged.current = false;
    drag.current = {
      pointerId: event.pointerId,
      origin: screenToFlowPosition({ x: event.clientX, y: event.clientY }),
      base: bend ?? { x: 0, y: 0 },
      moved: false,
    };
  };

  const dragMove = (event: ReactPointerEvent): void => {
    const current = drag.current;
    if (current?.pointerId !== event.pointerId || data.onBend === undefined) {
      return;
    }

    const now = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    const dx = now.x - current.origin.x;
    const dy = now.y - current.origin.y;
    if (!current.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) {
      return;
    }

    current.moved = true;
    data.onBend(id, { x: current.base.x + dx, y: current.base.y + dy });
  };

  const dragEnd = (event: ReactPointerEvent): void => {
    const current = drag.current;
    if (current?.pointerId !== event.pointerId) {
      return;
    }

    event.currentTarget.releasePointerCapture(event.pointerId);
    dragged.current = current.moved;
    drag.current = null;
  };

  const suppressClick = (event: ReactMouseEvent): void => {
    if (!dragged.current) {
      return;
    }

    dragged.current = false;
    event.stopPropagation();
  };

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        className={classes.join(" ")}
        markerEnd={`url(#${marker})`}
        interactionWidth={GRIP_WIDTH}
        style={undefined}
      />
      <path
        d={path}
        className="flow-edge__grip nopan"
        data-testid="flow-edge-grip"
        data-bent={bend !== undefined}
        fill="none"
        strokeWidth={GRIP_WIDTH}
        onPointerDown={dragStart}
        onPointerMove={dragMove}
        onPointerUp={dragEnd}
        onClickCapture={suppressClick}
      />
      {data.isHighlighted &&
        PARTICLES.map((delay) => (
          <circle key={delay} r={3} className="flow-edge__particle">
            <animateMotion dur="1.6s" begin={`${delay}s`} repeatCount="indefinite" path={path} />
          </circle>
        ))}
      <EdgeLabelRenderer>
        {showLabel && (
          <div
            className="flow-edge__label nopan"
            data-status={status}
            data-highlighted={data.isHighlighted}
            data-stale={data.stale.length > 0}
            data-selected={selected === true}
            data-testid="flow-edge-label"
            title={pairTitle}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
            onPointerDown={dragStart}
            onPointerMove={dragMove}
            onPointerUp={dragEnd}
            onClickCapture={suppressClick}
          >
            {data.label}
          </div>
        )}
      </EdgeLabelRenderer>
    </>
  );
}

function markerOf(highlighted: boolean, selected: boolean): string {
  if (selected) {
    return ARROW_MARKER_SELECTED;
  }

  if (highlighted) {
    return ARROW_MARKER_HIGHLIGHTED;
  }

  return ARROW_MARKER;
}

type Ends = Pick<
  EdgeProps,
  "sourceX" | "sourceY" | "targetX" | "targetY" | "sourcePosition" | "targetPosition"
>;

/** Путь линии и точка ярлыка. Без сдвига — кривая React Flow между ручками;
 * со сдвигом — две гладкие дуги через оттащенную середину, у ручек и в
 * середине касательная горизонтальна, ярлык стоит в середине. */
function bentPath(ends: Ends, bend: Position | undefined): [string, number, number] {
  if (bend === undefined) {
    const [path, labelX, labelY] = getBezierPath(ends);
    return [path, labelX, labelY];
  }

  const midX = (ends.sourceX + ends.targetX) / 2 + bend.x;
  const midY = (ends.sourceY + ends.targetY) / 2 + bend.y;
  const out = Math.max(Math.abs(midX - ends.sourceX) / 2, MIN_CONTROL);
  const into = Math.max(Math.abs(ends.targetX - midX) / 2, MIN_CONTROL);
  const first = `C ${ends.sourceX + out} ${ends.sourceY}, ${midX - out} ${midY}, ${midX} ${midY}`;
  const second = `C ${midX + into} ${midY}, ${ends.targetX - into} ${ends.targetY}, ${ends.targetX} ${ends.targetY}`;
  return [`M ${ends.sourceX} ${ends.sourceY} ${first} ${second}`, midX, midY];
}

/** Маркеры стрелок для рёбер: обычная, подсвеченная и выбранная; живут в одном svg на странице. */
export function ArrowMarkers(): ReactElement {
  return (
    <svg className="flow-markers" aria-hidden="true">
      <defs>
        <ArrowMarker id={ARROW_MARKER} className="flow-markers__arrow" />
        <ArrowMarker id={ARROW_MARKER_HIGHLIGHTED} className="flow-markers__arrow flow-markers__arrow--highlighted" />
        <ArrowMarker id={ARROW_MARKER_SELECTED} className="flow-markers__arrow flow-markers__arrow--selected" />
      </defs>
    </svg>
  );
}

function ArrowMarker({ id, className }: { id: string; className: string }): ReactElement {
  return (
    <marker id={id} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" className={className} />
    </marker>
  );
}
