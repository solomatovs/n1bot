import type { ToolResult } from "./workflow";

/** Сводка итога задачи по виду результата: цифра для узла и подпись к ней. */

export type ResultSummary = {
  kind: string;
  figure: string;
  detail: string;
};

function lineCount(text: string): number {
  if (text === "") {
    return 0;
  }

  return text.split("\n").length;
}

export function resultSummary(result: ToolResult): ResultSummary {
  switch (result.kind) {
    case "table":
      return { kind: result.kind, figure: String(result.rows.length), detail: "rows" };
    case "affected":
      return { kind: result.kind, figure: result.affected_rows === null ? "—" : String(result.affected_rows), detail: "rows" };
    case "shell":
      return { kind: result.kind, figure: `exit ${result.exit_code}`, detail: `${lineCount(result.stdout)} lines` };
    case "text":
      return { kind: result.kind, figure: String(lineCount(result.text)), detail: "lines" };
    case "json":
      return { kind: result.kind, figure: String(jsonSize(result.payload)), detail: "keys" };
    case "multi":
      return { kind: result.kind, figure: String(result.items.length), detail: "items" };
    case "chart":
      return { kind: result.kind, figure: "chart", detail: result.title ?? "" };
    case "diagram":
      return { kind: result.kind, figure: "diagram", detail: result.title ?? "" };
    case "custom_element":
      return { kind: result.kind, figure: result.element, detail: result.title ?? "" };
    case "error":
      return { kind: result.kind, figure: "✕", detail: result.error_kind };
    case "opaque":
      return { kind: result.kind, figure: "·", detail: "" };
  }
}

function jsonSize(payload: unknown): number {
  if (Array.isArray(payload)) {
    return payload.length;
  }

  if (typeof payload === "object" && payload !== null) {
    return Object.keys(payload).length;
  }

  return 1;
}

/** Подпись ребра-значения: что течёт из результата, `table ×12`. */
export function resultFlowLabel(result: ToolResult): string {
  const summary = resultSummary(result);
  switch (result.kind) {
    case "table":
    case "affected":
    case "multi":
    case "text":
    case "json":
      return `${summary.kind} ×${summary.figure}`;
    default:
      return summary.kind;
  }
}
