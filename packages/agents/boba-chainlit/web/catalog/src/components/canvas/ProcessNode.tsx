import { Handle, Position, type NodeProps } from "@xyflow/react";
import { KeyRound, TriangleAlert } from "lucide-react";
import type { ReactElement } from "react";

import { renderRef } from "../../model/catalog";
import type { ProcessFlowNode } from "../../model/graph";

/** Карточка узла: слой и подпись в шапке, родной вид объекта, колонки по
 * режиму показа из привязанной версии источника, статус diff рамкой, пометка
 * устаревания. Селектор data-node — адрес объекта, как в подписях сервера. */
export function ProcessNode({ data }: NodeProps<ProcessFlowNode>): ReactElement {
  const status = data.showDiff ? data.status : "unchanged";
  const stale = data.stale.length > 0;

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
      <Handle type="target" position={Position.Left} className="proc-node__handle" />
      <div className="proc-node__header">
        <span className="proc-node__layer">{data.layer?.name ?? "—"}</span>
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
            <li key={column.name} className="proc-node__column" data-nullable={column.nullable} data-key={column.key}>
              <span className="proc-node__column-icon">{column.key && <KeyRound size={11} />}</span>
              <span className="proc-node__column-name">{column.name}</span>
              <span className="proc-node__column-type">{column.type}</span>
            </li>
          ))}
        </ul>
      )}
      <Handle type="source" position={Position.Right} className="proc-node__handle" />
    </div>
  );
}
