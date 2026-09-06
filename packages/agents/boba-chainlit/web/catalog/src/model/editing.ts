import type { LayerChoice } from "../components/edit/LayerPicker";
import type { Flow, Layer, ObjectRef, ProcessNode } from "./catalog";
import type { CatalogOp } from "./ops";

/** Действия правки, которые страница отдаёт панелям на странице черновика;
 * страница открывает диалоги и шлёт операции, панели только зовут. */
export type EditActions = {
  apply: (ops: CatalogOp[]) => void;
  addLayer: () => void;
  renameLayer: (layer: Layer) => void;
  removeLayer: (layer: Layer) => void;
  /** Объект источника становится узлом слоя; новый слой заводится той же пачкой. */
  addNode: (layer: LayerChoice, ref: ObjectRef) => void;
  removeNode: (node: ProcessNode) => void;
  /** Узел переводится на другой объект; потоки остаются. */
  retargetNode: (node: ProcessNode, ref: ObjectRef) => void;
  newFlow: (from: ProcessNode) => void;
  editFlow: (flow: Flow) => void;
};
