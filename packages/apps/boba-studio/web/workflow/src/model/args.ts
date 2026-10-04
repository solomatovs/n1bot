import type { EditableEdge, EditableTask } from "./spec";
import { TEXT_EDITOR, type FieldEditor, type ToolFacts, type ToolResult } from "./workflow";

/** Строки аргументов блока: каталог задаёт порядок, редактор и показ, задача — значение,
 * рёбра-значения — привязку (что подставится вместо литерала). Чистая модель
 * для узла, формы и раскладки. */

export type ArgRow = {
  name: string;
  editor: FieldEditor;
  /** Объявленный показ значения; null — строкой как есть. */
  display: ToolResult | null;
  required: boolean;
  description: string;
  value: unknown;
  /** Источник ребра-значения вида `task.result`; пусто — литерал или ничего. */
  bound: string;
};

export type BlockRows = {
  intent: string;
  body: ArgRow[];
};

const INTENT = "intent";

export function boundSources(task: string, edges: EditableEdge[]): Map<string, string> {
  const bound = new Map<string, string>();
  for (const edge of edges) {
    if (edge.dst.task !== task || edge.dst.kind !== "arg") {
      continue;
    }

    bound.set(edge.dst.name, renderSource(edge));
  }

  return bound;
}

function renderSource(edge: EditableEdge): string {
  if (edge.src.kind === "result") {
    return `${edge.src.task}.result`;
  }

  return `${edge.src.task}.${edge.src.name}`;
}

export function blockRows(task: EditableTask, facts: ToolFacts | undefined, edges: EditableEdge[]): BlockRows {
  const bound = boundSources(task.name, edges);
  const body: ArgRow[] = [];
  const seen = new Set<string>();

  for (const arg of facts?.args ?? []) {
    seen.add(arg.name);
    if (arg.placement !== "body") {
      continue;
    }

    body.push({
      name: arg.name,
      editor: arg.editor,
      display: arg.display,
      required: arg.required,
      description: arg.description,
      value: task.args[arg.name],
      bound: bound.get(arg.name) ?? "",
    });
  }

  for (const [name, value] of Object.entries(task.args)) {
    if (seen.has(name) || name === INTENT) {
      continue;
    }

    body.push({
      name,
      editor: TEXT_EDITOR,
      display: null,
      required: false,
      description: "",
      value,
      bound: bound.get(name) ?? "",
    });
  }

  return { intent: intentOf(task), body };
}

export function intentOf(task: EditableTask): string {
  const raw = task.args[INTENT];
  if (typeof raw !== "string") {
    return "";
  }

  return raw;
}

export function withIntent(task: EditableTask, intent: string): EditableTask {
  if (intent === "") {
    const rest = Object.fromEntries(Object.entries(task.args).filter(([name]) => name !== INTENT));
    return { ...task, args: rest };
  }

  return { ...task, args: { ...task.args, [INTENT]: intent } };
}

/** Текст значения для однострочного показа; структуры — json одной строкой. */
export function valueText(value: unknown): string {
  if (value === undefined || value === null) {
    return "";
  }

  if (typeof value === "string") {
    return value;
  }

  return JSON.stringify(value);
}
