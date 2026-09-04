import type { ObjectRef } from "./catalog";
import { ObjectKindSchema } from "./catalog";

/** Адрес объекта в строке запроса: kind и ступени пути через JSON-массив,
 * чтобы имена с любыми символами доезжали как есть. */
export const RefParam = {
  render(ref: ObjectRef): string {
    return JSON.stringify([ref.kind, ...ref.path]);
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

    if (!Array.isArray(parsed) || parsed.length < 2) {
      return undefined;
    }

    const kind = ObjectKindSchema.safeParse(parsed[0]);
    if (!kind.success) {
      return undefined;
    }

    const path: string[] = [];
    for (const step of parsed.slice(1)) {
      if (typeof step !== "string") {
        return undefined;
      }

      path.push(step);
    }

    return { source_id: "", kind: kind.data, path };
  },
};
