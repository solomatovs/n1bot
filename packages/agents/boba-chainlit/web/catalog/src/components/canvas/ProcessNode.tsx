import { Handle, Position, type NodeProps } from "@xyflow/react";
import { KeyRound, TriangleAlert } from "lucide-react";
import type { ReactElement } from "react";

import { renderRef } from "../../model/catalog";
import { NODE_HANDLE, type ProcessFlowNode } from "../../model/graph";

/** Карточка узла: группа и подпись в шапке, родной вид объекта, колонки по
 * режиму показа из привязанной версии с ручками слева (приёмник) и справа
 * (источник) у каждой, статус diff рамкой, пометка устаревания; колонки, по
 * которым идут подсвеченные линии, подложены. Ручки карточки целиком держат
 * потоки без пар и принимают линии только в режиме имён, когда колонок на
 * карточке нет. Селектор data-node — адрес объекта. */
export function ProcessNode({ data }: NodeProps<ProcessFlowNode>): ReactElement {
  const status = data.showDiff ? data.status : "unchanged";
  const stale = data.stale.length > 0;
  // при видимых колонках линии соединяют колонки: ручки карточки целиком
  // только держат линии потоков без пар и не принимают новых
  const wholeCard = data.columns.length === 0;

  return (
    <div
      className="proc-node"
      data-status={status}
      data-active={data.isActive}
      data-highlighted={data.isHighlighted}
      data-stale={stale}
      data-testid="catalog-node"
      data-node={renderRef(data.node.ref)}
      data-label={data.label}
      data-kind={data.node.ref.kind}
    >
      <Handle
        type="target"
        id={NODE_HANDLE}
        position={Position.Left}
        className="proc-node__handle"
        isConnectable={wholeCard}
      />
      <div className="proc-node__header">
        <span className="proc-node__group">{data.group?.name ?? "—"}</span>
        <span className="proc-node__name">{data.label}</span>
        <span className="proc-node__kind">{data.node.ref.kind}</span>
        {stale && (
          <span className="proc-node__stale" title={data.stale.map((entry) => entry.reason).join(", ")}>
            <TriangleAlert size={12} />
          </span>
        )}
        {data.showDiff && status !== "unchanged" && <span className="proc-node__status">{status}</span>}
      </div>
      {data.columns.length > 0 && (
        <ul className="proc-node__columns">
          {data.columns.map((column) => (
            <li
              key={column.name}
              className="proc-node__column"
              data-column={column.name}
              data-nullable={column.nullable}
              data-key={column.key}
              data-lit={data.litColumns.has(column.name)}
            >
              <Handle
                type="target"
                id={column.name}
                position={Position.Left}
                className="proc-node__handle proc-node__handle--column"
              />
              <span className="proc-node__column-icon">{column.key && <KeyRound size={11} />}</span>
              <span className="proc-node__column-name">{column.name}</span>
              <span className="proc-node__column-type">{column.type}</span>
              <Handle
                type="source"
                id={column.name}
                position={Position.Right}
                className="proc-node__handle proc-node__handle--column"
              />
            </li>
          ))}
        </ul>
      )}
      <Handle
        type="source"
        id={NODE_HANDLE}
        position={Position.Right}
        className="proc-node__handle"
        isConnectable={wholeCard}
      />
    </div>
  );
}
