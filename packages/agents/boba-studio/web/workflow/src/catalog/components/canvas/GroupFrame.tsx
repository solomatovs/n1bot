import type { NodeProps } from "@xyflow/react";
import { Pencil, Trash2 } from "lucide-react";
import type { ReactElement } from "react";

import type { GroupNode } from "../../model/graph";
import { IconButton } from "../../../ui";

/** Рамка группы под карточками её узлов: имя, число узлов, статус diff у
 * добавленных и удалённых групп; на черновике — карандаш и корзина. Пока над
 * рамкой тащат карточку, она подсвечена: отпущенная войдёт в группу. */
export function GroupFrame({ data }: NodeProps<GroupNode>): ReactElement {
  const status = data.showDiff ? data.status : "unchanged";

  return (
    <div
      className="group-frame"
      data-status={status}
      data-drop={data.dropTarget}
      data-testid="group-frame"
      data-group={data.group.name}
    >
      <div className="group-frame__title">
        <span className="group-frame__name">{data.group.name}</span>
        <span className="group-frame__count">{data.count}</span>
        {data.onRename !== undefined && (
          <IconButton size="sm" ghost aria-label={`rename group ${data.group.name}`} onClick={data.onRename}>
            <Pencil size={12} />
          </IconButton>
        )}
        {data.onRemove !== undefined && (
          <IconButton size="sm" ghost aria-label={`remove group ${data.group.name}`} onClick={data.onRemove}>
            <Trash2 size={12} />
          </IconButton>
        )}
      </div>
    </div>
  );
}
