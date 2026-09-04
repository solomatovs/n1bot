import { useReactFlow } from "@xyflow/react";
import { LayoutGrid, Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import type { ReactElement } from "react";

import type { ShowMode } from "../model/graph";
import { IconButton, Segmented } from "../ui";

type Props = {
  showMode: ShowMode;
  onShowMode: (mode: ShowMode) => void;
  onTidy: () => void;
};

const MODE_OPTIONS: { value: ShowMode; label: string }[] = [
  { value: "ALL_FIELDS", label: "all fields" },
  { value: "KEY_ONLY", label: "keys" },
  { value: "TABLE_NAME", label: "names" },
];

/** Панель холста: масштаб, вписать, прибрать, режим карточек. Перенос Toolbar
 * из liam erd-core на виджеты страницы. */
export function CanvasToolbar({ showMode, onShowMode, onTidy }: Props): ReactElement {
  const { zoomIn, zoomOut, fitView } = useReactFlow();

  return (
    <div className="canvas-toolbar" data-testid="canvas-toolbar">
      <IconButton
        aria-label="zoom out"
        onClick={() => {
          void zoomOut();
        }}
      >
        <ZoomOut size={16} />
      </IconButton>
      <IconButton
        aria-label="zoom in"
        onClick={() => {
          void zoomIn();
        }}
      >
        <ZoomIn size={16} />
      </IconButton>
      <IconButton
        aria-label="fit view"
        onClick={() => {
          void fitView({ padding: 0.15, maxZoom: 1 });
        }}
      >
        <Maximize2 size={16} />
      </IconButton>
      <IconButton aria-label="tidy up" onClick={onTidy}>
        <LayoutGrid size={16} />
      </IconButton>
      <Segmented options={MODE_OPTIONS} value={showMode} onChange={onShowMode} label="show mode" />
    </div>
  );
}
