import { BaseEdge, EdgeLabelRenderer, getBezierPath, useReactFlow, type EdgeProps } from "@xyflow/react";
import { X } from "lucide-react";
import type { ReactElement } from "react";

import type { FlowEdge as FlowEdgeType } from "../../model/graph";
import { IconButton } from "../../ui";

export const ARROW_MARKER = "catalog-arrow";
export const ARROW_MARKER_HIGHLIGHTED = "catalog-arrow-highlighted";

const PARTICLES = [0, 0.55, 1.1];

/** Линия потока между колонками (или карточками): стрелка на конце, ярлык
 * потока на первой линии, при подсветке бегущие частицы, на черновике у
 * выбранной линии крестик, который снимает пару. Перенос RelationshipEdge
 * из liam erd-core. */
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
  const { deleteElements } = useReactFlow();
  if (data === undefined) {
    return null;
  }

  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });
  const status = data.showDiff ? data.status : "unchanged";
  const marker = data.isHighlighted ? ARROW_MARKER_HIGHLIGHTED : ARROW_MARKER;
  const pairTitle = data.pair === undefined ? "" : `${data.pair.from_column} → ${data.pair.to_column}`;
  const showLabel = data.label !== "";
  const showRemove = data.removable && selected === true;

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        className={data.isHighlighted ? "flow-edge flow-edge--lit" : "flow-edge"}
        markerEnd={`url(#${marker})`}
        interactionWidth={16}
        style={undefined}
      />
      {data.isHighlighted &&
        PARTICLES.map((delay) => (
          <circle key={delay} r={3} className="flow-edge__particle">
            <animateMotion dur="1.6s" begin={`${delay}s`} repeatCount="indefinite" path={path} />
          </circle>
        ))}
      <EdgeLabelRenderer>
        {(showLabel || showRemove) && (
          <div
            className="flow-edge__label"
            data-status={status}
            data-highlighted={data.isHighlighted}
            data-stale={data.stale.length > 0}
            data-selected={selected === true}
            data-testid={showLabel ? "flow-edge-label" : "flow-edge-tools"}
            title={pairTitle}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {showLabel && data.label}
            {showRemove && (
              <IconButton
                size="sm"
                ghost
                aria-label={data.pair === undefined ? "remove flow line" : `remove pair ${pairTitle}`}
                onClick={() => {
                  void deleteElements({ edges: [{ id }] });
                }}
              >
                <X size={11} />
              </IconButton>
            )}
          </div>
        )}
      </EdgeLabelRenderer>
    </>
  );
}

/** Маркеры стрелок для рёбер: обычная и подсвеченная; живут в одном svg на странице. */
export function ArrowMarkers(): ReactElement {
  return (
    <svg className="flow-markers" aria-hidden="true">
      <defs>
        <marker
          id={ARROW_MARKER}
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="8"
          markerHeight="8"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" className="flow-markers__arrow" />
        </marker>
        <marker
          id={ARROW_MARKER_HIGHLIGHTED}
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="8"
          markerHeight="8"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" className="flow-markers__arrow flow-markers__arrow--highlighted" />
        </marker>
      </defs>
    </svg>
  );
}
