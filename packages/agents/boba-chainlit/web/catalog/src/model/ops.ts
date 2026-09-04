import type { Flow, Layer, LoadKind, ObjectRef, ProcessNode } from "./catalog";

/** Операции над снимком процесса, как их принимает POST drafts/{id}/ops: add/set
 * несут сущность целиком, remove — только id, retarget — id и новый адрес.
 * Зеркало boba.catalog.ops. */
export type CatalogOp =
  | { op: "add_layer"; layer: Layer }
  | { op: "set_layer"; layer: Layer }
  | { op: "remove_layer"; id: string }
  | { op: "add_node"; node: ProcessNode }
  | { op: "set_node"; node: ProcessNode }
  | { op: "retarget_node"; id: string; ref: ObjectRef }
  | { op: "remove_node"; id: string }
  | { op: "add_load_kind"; load_kind: LoadKind }
  | { op: "set_load_kind"; load_kind: LoadKind }
  | { op: "remove_load_kind"; id: string }
  | { op: "add_flow"; flow: Flow }
  | { op: "set_flow"; flow: Flow }
  | { op: "remove_flow"; id: string };

export function newId(): string {
  return crypto.randomUUID();
}

/** Новый слой за последним по позиции. */
export function blankLayer(name: string, position: number): Layer {
  return { id: newId(), name, position, description: "" };
}

/** Узел из объекта источника в слое: без alias и заметки. */
export function blankNode(layerId: string, ref: ObjectRef): ProcessNode {
  return { id: newId(), layer_id: layerId, ref, alias: null, note: "" };
}

/** Поток между узлами без вида: вид и значения задаёт форма. */
export function blankFlow(from: string, to: string): Flow {
  return { id: newId(), from_node_id: from, to_node_id: to, load: { kind_id: "", values: {} }, description: "" };
}

export function blankLoadKind(name: string): LoadKind {
  return { id: newId(), name, description: "", fields: [] };
}

/** Удаление узла: сначала его потоки, иначе сервер откажет. */
export function removeNodeWithFlows(nodeId: string, flows: Flow[]): CatalogOp[] {
  const ops: CatalogOp[] = [];
  for (const flow of flows) {
    ops.push({ op: "remove_flow", id: flow.id });
  }
  ops.push({ op: "remove_node", id: nodeId });
  return ops;
}
