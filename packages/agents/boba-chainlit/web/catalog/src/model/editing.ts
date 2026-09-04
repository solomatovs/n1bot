import type { Dataset, Flow, Layer } from "./catalog";
import type { CatalogOp } from "./ops";

/** Действия правки, которые страница отдаёт панелям на странице черновика;
 * страница открывает диалоги и шлёт операции, панели только зовут. */
export type EditActions = {
  apply: (ops: CatalogOp[]) => void;
  addLayer: () => void;
  renameLayer: (layer: Layer) => void;
  removeLayer: (layer: Layer) => void;
  addDataset: (layerId: string) => void;
  removeDataset: (dataset: Dataset) => void;
  newFlow: (from: Dataset) => void;
  editFlow: (flow: Flow) => void;
};
