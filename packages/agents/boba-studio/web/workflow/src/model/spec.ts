import yaml from "js-yaml";
import { z } from "zod";

import type { EdgeKind, PortDirection, PortRef, PortRefKind } from "./workflow";

/** Редактируемая спека: YAML ↔ модель редактора. Грамматика портов и рёбер
 *  зеркалит boba.workflow.spec (PortRef, EdgeText), чтобы редактор и сервер
 *  читали одну и ту же запись. Без React и без сети. */

export type EditableTask = {
  name: string;
  tool: string;
  args: Record<string, unknown>;
  ports: Record<string, PortDirection>;
};

export type EditableEdge = {
  src: PortRef;
  dst: PortRef;
};

export type EditableWorkflow = {
  name: string;
  description: string;
  tasks: EditableTask[];
  edges: EditableEdge[];
};

export class SpecTextError extends Error {}

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;

const ARROW = "->";
const LIST_OPEN = "[";
const LIST_CLOSE = "]";
const LIST_SEP = ",";
const SEP = ".";
const RESULT = "result";
const ARGS = "args";

const SpecTextSchema = z.object({
  name: z.string(),
  description: z.string().default(""),
  tasks: z.record(
    z.object({
      tool: z.string(),
      args: z.record(z.unknown()).default({}),
      ports: z.record(z.enum(["read", "write"])).default({}),
    }),
  ),
  edges: z.array(z.string()).default([]),
});

export function isIdent(text: string): boolean {
  return IDENT.test(text);
}

/** Порт по записи `task`, `task.result`, `task.args.<имя>`, `task.<fd-порт>`. */
export function parsePortRef(raw: string): PortRef {
  const text = raw.trim();
  const dot = text.indexOf(SEP);
  const task = dot === -1 ? text : text.slice(0, dot);
  const rest = dot === -1 ? "" : text.slice(dot + 1);
  if (!isIdent(task)) {
    throw new SpecTextError(`bad port reference: ${raw}`);
  }

  if (rest === "") {
    return { task, kind: "task", name: "" };
  }

  if (rest === RESULT) {
    return { task, kind: "result", name: "" };
  }

  const restDot = rest.indexOf(SEP);
  if (restDot === -1) {
    return { task, kind: "fd", name: rest };
  }

  const head = rest.slice(0, restDot);
  const name = rest.slice(restDot + 1);
  if (head === ARGS && name !== "" && !name.includes(SEP)) {
    return { task, kind: "arg", name };
  }

  throw new SpecTextError(`expected task, task.result, task.args.<name> or task.<port>: ${raw}`);
}

export function renderPortRef(ref: PortRef): string {
  switch (ref.kind) {
    case "task":
      return ref.task;
    case "result":
      return `${ref.task}${SEP}${RESULT}`;
    case "arg":
      return `${ref.task}${SEP}${ARGS}${SEP}${ref.name}`;
    case "fd":
      return `${ref.task}${SEP}${ref.name}`;
  }
}

/** Вид ребра по портам; null — такая пара ребром не бывает. */
export function edgeKindOf(src: PortRefKind, dst: PortRefKind): EdgeKind | null {
  if (src === "fd" && dst === "fd") {
    return "stream";
  }

  if (src === "result" && dst === "arg") {
    return "value";
  }

  if (src === "task" && dst === "task") {
    return "control";
  }

  return null;
}

function parseSide(text: string, raw: string): PortRef[] {
  const stripped = text.trim();
  if (stripped === "") {
    throw new SpecTextError(`empty edge side: ${raw}`);
  }

  if (!stripped.startsWith(LIST_OPEN)) {
    return [parsePortRef(stripped)];
  }

  if (!stripped.endsWith(LIST_CLOSE)) {
    throw new SpecTextError(`unclosed '[': ${raw}`);
  }

  const inner = stripped.slice(LIST_OPEN.length, -LIST_CLOSE.length);
  return inner.split(LIST_SEP).map(parsePortRef);
}

/** Рёбра из строки; список с любой стороны раскрывается в произведение. */
export function parseEdgeText(raw: string): EditableEdge[] {
  const first = raw.indexOf(ARROW);
  if (first === -1 || raw.slice(first + ARROW.length).includes(ARROW)) {
    throw new SpecTextError(`expected exactly one '->': ${raw}`);
  }

  const sources = parseSide(raw.slice(0, first), raw);
  const targets = parseSide(raw.slice(first + ARROW.length), raw);

  const edges: EditableEdge[] = [];
  for (const src of sources) {
    for (const dst of targets) {
      edges.push({ src, dst });
    }
  }

  return edges;
}

export function renderEdge(edge: EditableEdge): string {
  return `${renderPortRef(edge.src)} ${ARROW} ${renderPortRef(edge.dst)}`;
}

export function edgeId(edge: EditableEdge): string {
  return renderEdge(edge);
}

export function parseSpecText(text: string): EditableWorkflow {
  let raw: unknown;
  try {
    raw = yaml.load(text);
  } catch (error: unknown) {
    throw new SpecTextError(`yaml: ${error instanceof Error ? error.message : String(error)}`);
  }

  const parsed = SpecTextSchema.safeParse(raw);
  if (!parsed.success) {
    throw new SpecTextError(`schema: ${parsed.error.issues.map((i) => i.message).join("; ")}`);
  }

  const tasks: EditableTask[] = Object.entries(parsed.data.tasks).map(([name, task]) => ({
    name,
    tool: task.tool,
    args: task.args,
    ports: task.ports,
  }));

  const edges = parsed.data.edges.flatMap(parseEdgeText);
  return { name: parsed.data.name, description: parsed.data.description, tasks, edges };
}

export function renderSpecText(workflow: EditableWorkflow): string {
  const tasks: Record<string, unknown> = {};
  for (const task of workflow.tasks) {
    const body: Record<string, unknown> = { tool: task.tool };
    if (Object.keys(task.args).length > 0) {
      body.args = task.args;
    }

    if (Object.keys(task.ports).length > 0) {
      body.ports = task.ports;
    }

    tasks[task.name] = body;
  }

  const data: Record<string, unknown> = { name: workflow.name };
  if (workflow.description !== "") {
    data.description = workflow.description;
  }

  data.tasks = tasks;
  if (workflow.edges.length > 0) {
    data.edges = workflow.edges.map(renderEdge);
  }

  return yaml.dump(data, { sortKeys: false, lineWidth: -1, noRefs: true });
}

/** Свободное имя задачи по инструменту: bash, bash_2, bash_3… */
export function freeTaskName(tool: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  if (!used.has(tool)) {
    return tool;
  }

  for (let index = 2; ; index += 1) {
    const candidate = `${tool}_${index}`;
    if (!used.has(candidate)) {
      return candidate;
    }
  }
}

/** Переименование задачи: рёбра и шаблоны аргументов следуют за именем. */
export function renameTask(workflow: EditableWorkflow, from: string, to: string): EditableWorkflow {
  const pattern = new RegExp(`\\{\\{\\s*${from}\\s*\\}\\}`, "g");
  const tasks = workflow.tasks.map((task) => {
    const args: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(task.args)) {
      args[key] = typeof value === "string" ? value.replace(pattern, `{{ ${to} }}`) : value;
    }

    return { ...task, name: task.name === from ? to : task.name, args };
  });

  const edges = workflow.edges.map((edge) => ({
    src: edge.src.task === from ? { ...edge.src, task: to } : edge.src,
    dst: edge.dst.task === from ? { ...edge.dst, task: to } : edge.dst,
  }));

  return { ...workflow, tasks, edges };
}

export function removeTask(workflow: EditableWorkflow, name: string): EditableWorkflow {
  return {
    ...workflow,
    tasks: workflow.tasks.filter((task) => task.name !== name),
    edges: workflow.edges.filter((edge) => edge.src.task !== name && edge.dst.task !== name),
  };
}

/** Замечание сервера: `code at where: message` либо `code: message`. */
export type SpecIssue = {
  code: string;
  where: string;
  message: string;
};

export function parseIssues(detail: string): SpecIssue[] {
  const issues: SpecIssue[] = [];
  for (const line of detail.split("; ")) {
    const colon = line.indexOf(": ");
    if (colon === -1) {
      issues.push({ code: "", where: "", message: line });
      continue;
    }

    const head = line.slice(0, colon);
    const message = line.slice(colon + 2);
    const at = head.indexOf(" at ");
    if (at === -1) {
      issues.push({ code: head, where: "", message });
      continue;
    }

    issues.push({ code: head.slice(0, at), where: head.slice(at + 4), message });
  }

  return issues;
}

/** Задачи, к которым относится замечание: по имени задачи или по рёбрам с ней. */
export function issueTasks(issue: SpecIssue, workflow: EditableWorkflow): Set<string> {
  const names = new Set(workflow.tasks.map((task) => task.name));
  const found = new Set<string>();
  if (names.has(issue.where)) {
    found.add(issue.where);
    return found;
  }

  for (const task of names) {
    if (issue.where.includes(`${task}${SEP}`) || issue.where.startsWith(`${task} `) || issue.where.endsWith(` ${task}`)) {
      found.add(task);
    }
  }

  return found;
}
