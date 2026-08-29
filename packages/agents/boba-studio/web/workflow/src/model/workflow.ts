import { z } from "zod";

import { RunStatusSchema, TaskStatusSchema } from "./status";

/** Модели зеркалят pydantic-модели boba.workflow и хранилища; разбор — на границе API. */

export const PortRefKindSchema = z.enum(["task", "result", "arg", "fd"]);
export type PortRefKind = z.infer<typeof PortRefKindSchema>;

export const PortRefSchema = z.object({
  task: z.string(),
  kind: PortRefKindSchema,
  name: z.string(),
});
export type PortRef = z.infer<typeof PortRefSchema>;

export const EdgeKindSchema = z.enum(["stream", "value", "control"]);
export type EdgeKind = z.infer<typeof EdgeKindSchema>;

export const EdgeSchema = z.object({
  src: PortRefSchema,
  dst: PortRefSchema,
  kind: EdgeKindSchema,
});
export type Edge = z.infer<typeof EdgeSchema>;

export const PortDirectionSchema = z.enum(["read", "write"]);
export type PortDirection = z.infer<typeof PortDirectionSchema>;

export const TaskSpecSchema = z.object({
  tool: z.string(),
  args: z.record(z.unknown()),
  ports: z.record(PortDirectionSchema),
});
export type TaskSpec = z.infer<typeof TaskSpecSchema>;

export const WorkflowSpecSchema = z.object({
  name: z.string(),
  description: z.string(),
  tasks: z.record(TaskSpecSchema),
  edges: z.array(EdgeSchema),
});
export type WorkflowSpec = z.infer<typeof WorkflowSpecSchema>;

export const StageSchema = z.object({
  id: z.string(),
  tasks: z.array(z.string()),
  streams: z.array(EdgeSchema),
  after: z.array(z.string()),
});
export type Stage = z.infer<typeof StageSchema>;

export const ArgBindingSchema = z.object({
  arg: z.string(),
  sources: z.array(z.string()),
  template: z.string(),
});
export type ArgBinding = z.infer<typeof ArgBindingSchema>;

export const WorkflowGraphSchema = z.object({
  spec: WorkflowSpecSchema,
  stages: z.array(StageSchema),
  bindings: z.record(z.array(ArgBindingSchema)),
});
export type WorkflowGraph = z.infer<typeof WorkflowGraphSchema>;

const resultBase = {
  ok: z.boolean(),
  elapsed_ms: z.number(),
  metadata: z.record(z.string()),
};

/** Итог инструмента — те же kind, что у ToolResult в boba.toolkit.result.
 * `opaque` — страничный вид для kind, которого страница не знает: сырой
 * итог целиком в payload (см. withKnownResults). */
export const ToolResultLeafSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("text"), ...resultBase, text: z.string(), language: z.string(), note: z.string().nullable() }),
  z.object({ kind: z.literal("json"), ...resultBase, payload: z.unknown() }),
  z.object({ kind: z.literal("table"), ...resultBase, rows: z.array(z.record(z.unknown())), note: z.string().nullable() }),
  z.object({
    kind: z.literal("affected"),
    ...resultBase,
    affected_rows: z.number().nullable(),
    status: z.string().nullable(),
  }),
  z.object({ kind: z.literal("chart"), ...resultBase, spec: z.record(z.unknown()), title: z.string().nullable() }),
  z.object({
    kind: z.literal("custom_element"),
    ...resultBase,
    element: z.string(),
    props: z.record(z.unknown()),
    title: z.string().nullable(),
  }),
  z.object({ kind: z.literal("diagram"), ...resultBase, spec: z.string(), path: z.string(), title: z.string().nullable() }),
  z.object({
    kind: z.literal("shell"),
    ...resultBase,
    exit_code: z.number(),
    stdout: z.string(),
    stdout_truncated: z.boolean(),
    stderr: z.string(),
    stderr_truncated: z.boolean(),
    duration_ms: z.number(),
    timed_out: z.boolean(),
    diagnostic: z.string(),
  }),
  z.object({ kind: z.literal("error"), ...resultBase, message: z.string(), error_kind: z.string() }),
  z.object({ kind: z.literal("opaque"), ...resultBase, payload: z.unknown() }),
]);
export type ToolResultLeaf = z.infer<typeof ToolResultLeafSchema>;

export type ToolResult =
  | ToolResultLeaf
  | { kind: "multi"; ok: boolean; elapsed_ms: number; metadata: Record<string, string>; items: ToolResult[] };

/** multi вложен рекурсивно: элементы — любые итоги, включая multi. */
export const ToolResultSchema: z.ZodType<ToolResult> = z.lazy(() =>
  z.union([z.object({ kind: z.literal("multi"), ...resultBase, items: z.array(ToolResultSchema) }), ToolResultLeafSchema]),
);

const KNOWN_RESULT_KINDS = new Set<string>([...ToolResultLeafSchema.options.map((option) => option.shape.kind.value), "multi"]);

const RawResultSchema = z.object({ kind: z.string(), ...resultBase }).passthrough();

function knownResult(raw: unknown): unknown {
  const parsed = RawResultSchema.safeParse(raw);
  if (!parsed.success) {
    return raw;
  }

  if (!KNOWN_RESULT_KINDS.has(parsed.data.kind)) {
    const { ok, elapsed_ms, metadata } = parsed.data;
    return { kind: "opaque", ok, elapsed_ms, metadata, payload: raw };
  }

  if (parsed.data.kind === "multi" && Array.isArray(parsed.data.items)) {
    return { ...parsed.data, items: parsed.data.items.map(knownResult) };
  }

  return raw;
}

const RawTasksSchema = z.object({ tasks: z.record(z.object({ result: z.unknown() }).passthrough()) }).passthrough();

/** Неизвестный kind итога (бэкенд новее страницы) не ломает разбор запуска:
 * такой итог становится opaque. Принимает состояние запуска или запись с state. */
export function withKnownResults(raw: unknown): unknown {
  if (typeof raw !== "object" || raw === null) {
    return raw;
  }

  if ("state" in raw) {
    return { ...raw, state: withKnownResults(raw.state) };
  }

  const state = RawTasksSchema.safeParse(raw);
  if (!state.success) {
    return raw;
  }

  const tasks: Record<string, unknown> = {};
  for (const [name, task] of Object.entries(state.data.tasks)) {
    tasks[name] = { ...task, result: task.result === null ? null : knownResult(task.result) };
  }

  return { ...state.data, tasks };
}

export const TaskStateSchema = z.object({
  status: TaskStatusSchema,
  call_id: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  error: z.string(),
  result: ToolResultSchema.nullable(),
});
export type TaskState = z.infer<typeof TaskStateSchema>;

export const RunStateSchema = z.object({
  graph: WorkflowGraphSchema,
  status: RunStatusSchema,
  tasks: z.record(TaskStateSchema),
});
export type RunState = z.infer<typeof RunStateSchema>;

export const StoredWorkflowSchema = z.object({
  id: z.number().int(),
  user_id: z.number().int(),
  name: z.string(),
  spec: z.string(),
  tools: z.array(z.string()),
  layout: z.record(z.unknown()),
  created_at: z.string(),
  updated_at: z.string(),
});
export type StoredWorkflow = z.infer<typeof StoredWorkflowSchema>;

/** Черновик билдера, общий для вкладок пользователя: последняя запись побеждает. */
export const WorkflowDraftSchema = z.object({
  key: z.string(),
  user_id: z.number().int(),
  revision: z.number().int(),
  spec: z.string(),
  layout: z.record(z.unknown()),
  updated_at: z.string(),
});
export type WorkflowDraft = z.infer<typeof WorkflowDraftSchema>;

export const InitiatorSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("chat"), thread_id: z.string(), turn_id: z.string() }),
  z.object({ kind: z.literal("llm"), thread_id: z.string(), tool_call_id: z.string() }),
  z.object({ kind: z.literal("human"), via: z.enum(["page", "api"]) }),
  z.object({ kind: z.literal("schedule"), job_id: z.string(), job_run_id: z.string() }),
]);
export type Initiator = z.infer<typeof InitiatorSchema>;

export const StoredRunSchema = z.object({
  id: z.string().uuid(),
  workflow_id: z.number().int().nullable(),
  user_id: z.number().int(),
  initiator: InitiatorSchema,
  profile: z.string(),
  state: RunStateSchema,
  instance: z.string(),
  started_at: z.string(),
  finished_at: z.string().nullable(),
  status: RunStatusSchema,
});
export type StoredRun = z.infer<typeof StoredRunSchema>;

export const RunSnapshotSchema = z.object({
  run_id: z.string().uuid(),
  status: RunStatusSchema,
  state: RunStateSchema,
});
export type RunSnapshot = z.infer<typeof RunSnapshotSchema>;

export const ToolAvailabilitySchema = z.enum(["available", "denied", "chat_only"]);
export type ToolAvailability = z.infer<typeof ToolAvailabilitySchema>;

export const ArgPlacementSchema = z.enum(["body", "header", "hidden"]);
export type ArgPlacement = z.infer<typeof ArgPlacementSchema>;

const placed = { placement: ArgPlacementSchema };

/** Виды аргумента — те же kind, что у ArgView в boba.toolkit.calls. */
export const ArgViewSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("text"), ...placed, multiline: z.boolean(), placeholder: z.string() }),
  z.object({ kind: z.literal("code"), ...placed, lang: z.string() }),
  z.object({ kind: z.literal("connection"), ...placed, family: z.string() }),
  z.object({ kind: z.literal("enum"), ...placed, options: z.array(z.string()) }),
  z.object({
    kind: z.literal("number"),
    ...placed,
    minimum: z.number().nullable(),
    maximum: z.number().nullable(),
    unit: z.string(),
  }),
  z.object({ kind: z.literal("bool"), ...placed }),
  z.object({ kind: z.literal("path"), ...placed }),
  z.object({ kind: z.literal("json"), ...placed }),
  z.object({ kind: z.literal("secret"), ...placed }),
  z.object({ kind: z.literal("intent"), ...placed }),
]);
export type ArgView = z.infer<typeof ArgViewSchema>;
export type ArgKind = ArgView["kind"];

export const TEXT_VIEW: ArgView = { kind: "text", placement: "body", multiline: false, placeholder: "" };

export const ToolArgSchema = z.object({
  name: z.string(),
  required: z.boolean(),
  view: ArgViewSchema,
  description: z.string(),
});
export type ToolArg = z.infer<typeof ToolArgSchema>;

const RawArgSchema = z.object({ view: z.unknown() }).passthrough();
const RawFactsSchema = z.object({ args: z.array(RawArgSchema) }).passthrough();

/** Неизвестный kind (бэкенд новее страницы) не ломает каталог: такой вид
 * подменяется текстом до строгого разбора. */
export function looseViews(raw: unknown): unknown {
  const facts = z.record(RawFactsSchema).safeParse(raw);
  if (!facts.success) {
    return raw;
  }

  const patched: Record<string, unknown> = {};
  for (const [name, tool] of Object.entries(facts.data)) {
    const args = tool.args.map((arg) => {
      if (ArgViewSchema.safeParse(arg.view).success) {
        return arg;
      }

      return { ...arg, view: TEXT_VIEW };
    });
    patched[name] = { ...tool, args };
  }

  return patched;
}

export const ToolPortSchema = z.object({ name: z.string(), direction: PortDirectionSchema });
export type ToolPort = z.infer<typeof ToolPortSchema>;

export const ToolFactsSchema = z.object({
  name: z.string(),
  availability: ToolAvailabilitySchema,
  description: z.string(),
  args: z.array(ToolArgSchema),
  ports: z.array(ToolPortSchema),
  results: z.array(z.string()),
  task_ports: z.boolean(),
});
export type ToolFacts = z.infer<typeof ToolFactsSchema>;

export const ToolCatalogSchema = z.record(ToolFactsSchema);
export type ToolCatalog = z.infer<typeof ToolCatalogSchema>;

export const RunStartedSchema = z.object({ run_id: z.string().uuid() });
export const StopOutcomeSchema = z.enum(["stopped", "accepted", "finished"]);
export type StopOutcome = z.infer<typeof StopOutcomeSchema>;
export const StoppedSchema = z.object({ outcome: StopOutcomeSchema });
export const DeletedSchema = z.object({ deleted: z.boolean() });

/** Окно журнала вывода стадии: текст и координаты в файле. */
export const StreamSliceSchema = z.object({
  text: z.string(),
  offset: z.number(),
  end: z.number(),
  size: z.number(),
  window: z.number(),
  closed: z.boolean(),
  note: z.string(),
});
export type StreamSlice = z.infer<typeof StreamSliceSchema>;

/** Канал журнала стадии: имя для запроса окна и подпись вкладки. */
export const ChannelViewSchema = z.object({
  name: z.string(),
  label: z.string(),
});
export type ChannelView = z.infer<typeof ChannelViewSchema>;

/** Событие журнала стадии из шины (socket.io `stream_event`): канал вызова дорос до size байт
 * или закрыт; сам текст читается окнами через REST. */
export const StreamEventSchema = z.object({
  run_id: z.string(),
  call_id: z.string(),
  channel: z.string(),
  size: z.number(),
  closed: z.boolean(),
  note: z.string(),
});
export type StreamEvent = z.infer<typeof StreamEventSchema>;

/** Событие ленты пользователя из шины (socket.io `user_event`): те же поля, что у сообщений
 * RunListChanged, WorkflowChanged, ConnectionsChanged на сервере. */
export const UserEventSchema = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("run_list_changed"),
    run_id: z.string(),
    workflow_id: z.number().nullable(),
    workflow_name: z.string(),
    status: z.string(),
  }),
  z.object({
    kind: z.literal("workflow_changed"),
    workflow_id: z.number(),
    name: z.string(),
    action: z.enum(["created", "updated", "deleted"]),
  }),
  z.object({
    kind: z.literal("workflow_draft_changed"),
    key: z.string(),
    revision: z.number().int(),
    by_sid: z.string(),
    action: z.enum(["created", "updated", "deleted"]),
  }),
  z.object({
    kind: z.literal("studio_profile_changed"),
    profile: z.string(),
    by_sid: z.string(),
  }),
  z.object({
    kind: z.literal("connections_changed"),
    connection_id: z.number(),
    name: z.string(),
    action: z.enum(["created", "updated", "deleted"]),
  }),
]);
export type UserEvent = z.infer<typeof UserEventSchema>;
