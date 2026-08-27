import { Position, type NodeHandle } from "@xyflow/react";

/** Геометрия handle узла, заданная явно: React Flow позиционирует рёбра по ней
 * сразу, не дожидаясь измерения DOM (иначе рёбра зависят от гонки измерений).
 * Размер совпадает с .react-flow__handle в app.css; handle центрирован на точке. */
export const HANDLE_SIZE = 9;

export function sideHandle(
  type: NodeHandle["type"],
  position: Position.Left | Position.Right,
  centerX: number,
  centerY: number,
  id?: string,
): NodeHandle {
  return {
    id: id ?? null,
    type,
    position,
    x: centerX - HANDLE_SIZE / 2,
    y: centerY - HANDLE_SIZE / 2,
    width: HANDLE_SIZE,
    height: HANDLE_SIZE,
  };
}
