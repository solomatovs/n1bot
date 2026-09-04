import type { Column, Dataset, Flow, Layer, LoadKind } from "./catalog";

/** Операции над снимком, как их принимает POST drafts/{id}/ops: add/set несут
 * сущность целиком, remove — только id. Зеркало boba.catalog.ops. */
export type CatalogOp =
  | { op: "add_layer"; layer: Layer }
  | { op: "set_layer"; layer: Layer }
  | { op: "remove_layer"; id: string }
  | { op: "add_dataset"; dataset: Dataset }
  | { op: "set_dataset"; dataset: Dataset }
  | { op: "remove_dataset"; id: string }
  | { op: "add_column"; column: Column }
  | { op: "set_column"; column: Column }
  | { op: "remove_column"; id: string }
  | { op: "add_load_kind"; load_kind: LoadKind }
  | { op: "set_load_kind"; load_kind: LoadKind }
  | { op: "remove_load_kind"; id: string }
  | { op: "add_flow"; flow: Flow }
  | { op: "set_flow"; flow: Flow }
  | { op: "remove_flow"; id: string };

export function newId(): string {
  return crypto.randomUUID();
}

/** Пустой набор в слое: имя задаёт пользователь, остальное пусто. */
export function blankDataset(layerId: string, name: string): Dataset {
  return { id: newId(), layer_id: layerId, name, source: "", description: "", tags: [], owner: "" };
}

/** Пустая колонка набора на следующей позиции. */
export function blankColumn(datasetId: string, position: number): Column {
  return {
    id: newId(),
    dataset_id: datasetId,
    name: "",
    type: "text",
    nullable: true,
    is_key: false,
    position,
    description: "",
  };
}

/** Удаление набора: сначала его потоки, иначе сервер откажет. */
export function removeDatasetWithFlows(datasetId: string, flows: Flow[]): CatalogOp[] {
  const ops: CatalogOp[] = [];
  for (const flow of flows) {
    ops.push({ op: "remove_flow", id: flow.id });
  }
  ops.push({ op: "remove_dataset", id: datasetId });
  return ops;
}
