import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@xyflow/react";
import type { ReactElement } from "react";

import type { FlowEdge as FlowEdgeType } from "../../model/graph";

export const ARROW_MARKER = "catalog-arrow";
export const ARROW_MARKER_HIGHLIGHTED = "catalog-arrow-highlighted";

/** Ребро потока: стрелка вместо маркеров кардинальности, подпись — вид загрузки,
 * при подсветке бегущие частицы. Перенос RelationshipEdge из liam erd-core. */
export function FlowEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps<FlowEdgeType>): ReactElement | null {
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

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        className="flow-edge"
        markerEnd={`url(#${marker})`}
        interactionWidth={16}
        style={undefined}
      />
      {data.isHighlighted && (
        <circle r={3} className="flow-edge__particle">
          <animateMotion dur="1.6s" repeatCount="indefinite" path={path} />
        </circle>
      )}
      <EdgeLabelRenderer>
        <div
          className="flow-edge__label"
          data-status={status}
          data-highlighted={data.isHighlighted}
          data-testid="flow-edge-label"
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          {data.loadKind}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

/** Маркеры стрелок для рёбер: обычная и подсвеченная; живут в одном svg на странице. */
export function ArrowMarkers(): ReactElement {
  return (
    <svg className="flow-markers" aria-hidden="true">
      <defs>
        <marker id={ARROW_MARKER} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
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
