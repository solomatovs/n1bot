import type { Flow, Group, ObjectRef, Position, ProcessNode } from "./catalog";

/** Операции над снимком процесса, как их принимает POST drafts/{id}/ops: add/set
 * несут сущность целиком, remove — только id, retarget — id и новый адрес.
 * Зеркало boba.catalog.ops. */
export type CatalogOp =
  | { op: "add_group"; group: Group }
  | { op: "set_group"; group: Group }
  | { op: "remove_group"; id: string }
  | { op: "add_node"; node: ProcessNode }
  | { op: "set_node"; node: ProcessNode }
  | { op: "retarget_node"; id: string; ref: ObjectRef }
  | { op: "remove_node"; id: string }
  | { op: "add_flow"; flow: Flow }
  | { op: "set_flow"; flow: Flow }
  | { op: "remove_flow"; id: string };

export function newId(): string {
  return crypto.randomUUID();
}

/** Новая именованная группа без узлов. */
export function blankGroup(name: string): Group {
  return { id: newId(), name };
}

/** Узел из объекта подключения на холсте: без alias, заметки и группы;
 * без позиции его разложит страница, ширина карточки стандартная. */
export function blankNode(ref: ObjectRef, position: Position | null): ProcessNode {
  return { id: newId(), ref, position, width: null, group_id: null, alias: null, note: "" };
}

/** Удаление группы: сначала её узлы выводятся из неё, иначе сервер откажет. */
export function removeGroupWithNodes(groupId: string, nodes: ProcessNode[]): CatalogOp[] {
  const ops: CatalogOp[] = [];
  for (const node of nodes) {
    ops.push({ op: "set_node", node: { ...node, group_id: null } });
  }
  ops.push({ op: "remove_group", id: groupId });
  return ops;
}

/** Поток между узлами без пар колонок: пары задаёт форма. */
export function blankFlow(from: string, to: string): Flow {
  return { id: newId(), from_node_id: from, to_node_id: to, columns: [], description: "" };
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
