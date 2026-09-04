import { Handle, Position, type NodeProps } from "@xyflow/react";
import { KeyRound } from "lucide-react";
import type { ReactElement } from "react";

import type { DatasetNode as DatasetFlowNode } from "../../model/graph";

/** Карточка набора: слой и имя в шапке, колонки по режиму показа, статус diff
 * рамкой. Перенос TableNode из liam erd-core без иконки внешнего ключа. */
export function DatasetNode({ data }: NodeProps<DatasetFlowNode>): ReactElement {
  const status = data.showDiff ? data.status : "unchanged";

  return (
    <div
      className="ds-node"
      data-status={status}
      data-active={data.isActive}
      data-highlighted={data.isHighlighted}
      data-testid="dataset-node"
      data-dataset={data.dataset.name}
    >
      <Handle type="target" position={Position.Left} className="ds-node__handle" />
      <div className="ds-node__header">
        <span className="ds-node__layer">{data.layer?.name ?? "—"}</span>
        <span className="ds-node__name">{data.dataset.name}</span>
        {data.showDiff && status !== "unchanged" && <span className="ds-node__status">{status}</span>}
      </div>
      {data.columns.length > 0 && (
        <ul className="ds-node__columns">
          {data.columns.map((column) => (
            <li
              key={column.id}
              className="ds-node__column"
              data-nullable={column.nullable}
              data-key={column.is_key}
            >
              <span className="ds-node__column-icon">{column.is_key && <KeyRound size={11} />}</span>
              <span className="ds-node__column-name">{column.name}</span>
              <span className="ds-node__column-type">{column.type}</span>
            </li>
          ))}
        </ul>
      )}
      <Handle type="source" position={Position.Right} className="ds-node__handle" />
    </div>
  );
}
