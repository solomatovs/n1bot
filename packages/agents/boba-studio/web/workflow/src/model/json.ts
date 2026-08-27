/** Разбор произвольного JSON-значения в строки для структурного показа:
 * ключ, вид значения, текст; вложенность — глубиной. Чистая модель без DOM. */

export type JsonRowKind = "open" | "close" | "empty" | "string" | "number" | "boolean" | "null";

export type JsonRow = {
  depth: number;
  key: string;
  kind: JsonRowKind;
  text: string;
};

const ELLIPSIS = "…";

/** Строка в одну линию не длиннее clip символов; clip = 0 — без обрезки. */
export function clipText(text: string, clip: number): string {
  if (clip === 0) {
    return text;
  }

  const firstLine = text.split("\n")[0] ?? "";
  const cut = firstLine.length < text.length;
  if (firstLine.length <= clip && !cut) {
    return firstLine;
  }

  return `${firstLine.slice(0, clip)}${ELLIPSIS}`;
}

export function jsonRows(value: unknown, clip: number): JsonRow[] {
  const rows: JsonRow[] = [];
  walk(value, "", 0, clip, rows);
  return rows;
}

function walk(value: unknown, key: string, depth: number, clip: number, rows: JsonRow[]): void {
  if (Array.isArray(value)) {
    if (value.length === 0) {
      rows.push({ depth, key, kind: "empty", text: "[]" });
      return;
    }

    rows.push({ depth, key, kind: "open", text: "[" });
    for (const item of value) {
      walk(item, "", depth + 1, clip, rows);
    }
    rows.push({ depth, key: "", kind: "close", text: "]" });
    return;
  }

  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      rows.push({ depth, key, kind: "empty", text: "{}" });
      return;
    }

    rows.push({ depth, key, kind: "open", text: "{" });
    for (const [name, item] of entries) {
      walk(item, name, depth + 1, clip, rows);
    }
    rows.push({ depth, key: "", kind: "close", text: "}" });
    return;
  }

  rows.push({ depth, key, ...scalar(value, clip) });
}

function scalar(value: unknown, clip: number): { kind: JsonRowKind; text: string } {
  switch (typeof value) {
    case "string":
      return { kind: "string", text: clipText(value, clip) };
    case "number":
      return { kind: "number", text: String(value) };
    case "boolean":
      return { kind: "boolean", text: String(value) };
    default:
      return { kind: "null", text: "null" };
  }
}
