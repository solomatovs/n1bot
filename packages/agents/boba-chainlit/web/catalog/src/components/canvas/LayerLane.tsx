import type { NodeProps } from "@xyflow/react";
import type { ReactElement } from "react";

import type { LayerNode } from "../../model/graph";

/** Дорожка слоя под карточками его наборов: рамка и подпись; статус diff у
 * добавленных и удалённых слоёв. Аналог NonRelatedTableGroupNode из liam. */
export function LayerLane({ data }: NodeProps<LayerNode>): ReactElement {
  const status = data.showDiff ? data.status : "unchanged";

  return (
    <div className="layer-lane" data-status={status} data-testid="layer-lane" data-layer={data.layer.name}>
      <div className="layer-lane__title">
        <span className="layer-lane__name">{data.layer.name}</span>
        <span className="layer-lane__count">{data.count}</span>
      </div>
    </div>
  );
}
