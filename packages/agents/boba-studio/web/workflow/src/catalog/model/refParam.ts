import type { ObjectRef } from "./catalog";
import { ObjectKindSchema } from "./catalog";

/** Тип данных перетаскивания объекта из дерева источника на холст: в нём
 * лежит адрес в форме ObjectRefParam. */
export const OBJECT_DRAG_TYPE = "application/x-boba-object";

/** Адрес объекта одной строкой — для строки запроса и перетаскивания:
 * подключение, kind и ступени пути JSON-массивом, чтобы имена с любыми
 * символами доезжали как есть. */
export const ObjectRefParam = {
  render(ref: ObjectRef): string {
    return JSON.stringify([ref.connection_id, ref.kind, ...ref.path]);
  },

  parse(raw: string | null): ObjectRef | undefined {
    if (raw === null || raw === "") {
      return undefined;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return undefined;
    }

    if (!Array.isArray(parsed) || parsed.length < 3) {
      return undefined;
    }

    const [connectionId, rawKind, ...steps] = parsed as unknown[];
    if (typeof connectionId !== "string") {
      return undefined;
    }

    const kind = ObjectKindSchema.safeParse(rawKind);
    if (!kind.success) {
      return undefined;
    }

    const path: string[] = [];
    for (const step of steps) {
      if (typeof step !== "string") {
        return undefined;
      }

      path.push(step);
    }

    return { connection_id: connectionId, kind: kind.data, path };
  },
};
