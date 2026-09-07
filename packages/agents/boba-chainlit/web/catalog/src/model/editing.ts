import type { ColumnLink, Flow, Group, ObjectRef, Position, ProcessNode } from "./catalog";
import type { CatalogOp } from "./ops";

export type NodeMove = { node: ProcessNode; position: Position; groupId: string | null };

/** Соединение ручек на холсте: карточки и, если тянули от колонки к колонке, пара. */
export type Connection = { from: string; to: string; fromColumn: string | undefined; toColumn: string | undefined };

/** Снятая с холста линия: пара потока либо поток целиком. */
export type LinkRemoval = { flowId: string; pair: ColumnLink | undefined };

/** Действия правки, которые страница отдаёт панелям и холсту на странице
 * черновика; страница открывает диалоги и шлёт операции, панели только зовут. */
export type EditActions = {
  apply: (ops: CatalogOp[]) => void;
  /** Группа из узлов: имя спросит страница. */
  groupNodes: (nodes: ProcessNode[]) => void;
  renameGroup: (group: Group) => void;
  /** Группа снимается, узлы остаются на холсте без группы. */
  removeGroup: (group: Group) => void;
  /** Объект подключения становится узлом на холсте; без позиции его разложит
   * холст, брошенный в рамку сразу входит в группу. */
  addNode: (ref: ObjectRef, position: Position | null, groupId: string | null) => void;
  /** Узлы передвинуты одной пачкой; попавший в чужую рамку меняет группу,
   * вытащенный из своей — теряет её. */
  moveNodes: (moves: NodeMove[]) => void;
  removeNode: (node: ProcessNode) => void;
  /** Узел переводится на другой объект; потоки остаются. */
  retargetNode: (node: ProcessNode, ref: ObjectRef) => void;
  newFlow: (from: ProcessNode) => void;
  editFlow: (flow: Flow) => void;
  /** Линия от колонки к колонке добавляет пару в поток между карточками
   * (заводит поток, если его нет); линия карточка → карточка открывает форму. */
  connect: (connection: Connection) => void;
  /** Снятые линии: пары уходят из потоков, поток без пар исчезает. */
  removeLinks: (removed: LinkRemoval[]) => void;
};
