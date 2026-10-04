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

/** Итог инструмента: kind и поля базы; остальное — данные конкретного вида,
 * страница их не разбирает: показ приходит готовым StudioView в task.view. */
export const ToolResultSchema = z.object({ kind: z.string(), ...resultBase }).passthrough();
export type ToolResult = z.infer<typeof ToolResultSchema>;

export const FactSchema = z.object({ key: z.string(), value: z.string() });

/** Словарь блоков страницы — тот же, что StudioBlock в boba.toolkit.result. */
export const StudioBlockSchema = z.discriminatedUnion("block", [
  z.object({ block: z.literal("code"), text: z.string(), language: z.string() }),
  z.object({ block: z.literal("grid"), rows: z.array(z.record(z.unknown())) }),
  z.object({ block: z.literal("facts"), facts: z.array(FactSchema) }),
  z.object({ block: z.literal("note"), text: z.string() }),
  z.object({ block: z.literal("widget"), element: z.string(), props: z.record(z.unknown()), title: z.string() }),
]);
export type StudioBlock = z.infer<typeof StudioBlockSchema>;

export const StudioSummarySchema = z.object({ figure: z.string(), detail: z.string() });
export type StudioSummary = z.infer<typeof StudioSummarySchema>;

export const StudioViewSchema = z.object({
  summary: StudioSummarySchema,
  blocks: z.array(StudioBlockSchema),
});
export type StudioView = z.infer<typeof StudioViewSchema>;

export const TaskStateSchema = z.object({
  status: TaskStatusSchema,
  call_id: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  error: z.string(),
  result: ToolResultSchema.nullable(),
  view: StudioViewSchema.nullable(),
});
export type TaskState = z.infer<typeof TaskStateSchema>;

export const RunStateSchema = z.object({
  graph: WorkflowGraphSchema,
  status: RunStatusSchema,
  tasks: z.record(TaskStateSchema),
});
export type RunState = z.infer<typeof RunStateSchema>;

export const StoredWorkflowSchema = z.object({
  id: z.string().uuid(),
  user_id: z.string().uuid(),
  name: z.string(),
  spec: z.string(),
  tools: z.array(z.string()),
  layout: z.record(z.unknown()),
  draft_spec: z.string().nullable(),
  draft_layout: z.record(z.unknown()).nullable(),
  draft_revision: z.number().int(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type StoredWorkflow = z.infer<typeof StoredWorkflowSchema>;

export const InitiatorSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("chat"), thread_id: z.string(), turn_id: z.string() }),
  z.object({ kind: z.literal("llm"), thread_id: z.string(), tool_call_id: z.string() }),
  z.object({ kind: z.literal("human"), via: z.enum(["page", "api"]) }),
  z.object({ kind: z.literal("schedule"), job_id: z.string(), job_run_id: z.string() }),
]);
export type Initiator = z.infer<typeof InitiatorSchema>;

export const StoredRunSchema = z.object({
  id: z.string().uuid(),
  workflow_id: z.string().uuid().nullable(),
  user_id: z.string().uuid(),
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

export const ToolAvailabilitySchema = z.enum(["available", "denied", "headless_only"]);
export type ToolAvailability = z.infer<typeof ToolAvailabilitySchema>;

export const FieldPlacementSchema = z.enum(["body", "header", "hidden"]);
export type FieldPlacement = z.infer<typeof FieldPlacementSchema>;

/** Редакторы поля формы — те же, что FieldEditor в boba.toolkit.calls. */
export const FieldEditorSchema = z.discriminatedUnion("editor", [
  z.object({ editor: z.literal("text"), multiline: z.boolean(), placeholder: z.string() }),
  z.object({ editor: z.literal("number"), minimum: z.number().nullable(), maximum: z.number().nullable() }),
  z.object({ editor: z.literal("select"), options: z.array(z.string()) }),
  z.object({ editor: z.literal("bool") }),
  z.object({ editor: z.literal("connection"), family: z.string() }),
  z.object({ editor: z.literal("json") }),
  z.object({ editor: z.literal("secret") }),
]);
export type FieldEditor = z.infer<typeof FieldEditorSchema>;
export type EditorKind = FieldEditor["editor"];

export const TEXT_EDITOR: FieldEditor = { editor: "text", multiline: false, placeholder: "" };

/** Поле формы задачи: редактор из типа, показ значения — объявленный результат. */
export const StudioFieldSchema = z.object({
  name: z.string(),
  description: z.string(),
  required: z.boolean(),
  placement: FieldPlacementSchema,
  editor: FieldEditorSchema,
  display: ToolResultSchema.nullable(),
});
export type StudioField = z.infer<typeof StudioFieldSchema>;

const RawFieldSchema = z.object({ editor: z.unknown() }).passthrough();
const RawFactsSchema = z.object({ args: z.array(RawFieldSchema) }).passthrough();

/** Неизвестный редактор (бэкенд новее страницы) не ломает каталог: он
 * подменяется текстовым до строгого разбора. */
export function looseEditors(raw: unknown): unknown {
  const facts = z.record(RawFactsSchema).safeParse(raw);
  if (!facts.success) {
    return raw;
  }

  const patched: Record<string, unknown> = {};
  for (const [name, tool] of Object.entries(facts.data)) {
    const args = tool.args.map((arg) => {
      if (FieldEditorSchema.safeParse(arg.editor).success) {
        return arg;
      }

      return { ...arg, editor: TEXT_EDITOR };
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
  args: z.array(StudioFieldSchema),
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

/** Каталог данных изменился: заполнен ровно один из идентификаторов —
 * процесс (с version — опубликована версия), черновик, подключение,
 * синхронизация или upgrade; action — что с ним случилось. */
export const CatalogChangedSchema = z.object({
  kind: z.literal("catalog_changed"),
  process_id: z.string().nullable().default(null),
  version: z.number().nullable().default(null),
  draft_id: z.string().nullable().default(null),
  connection_id: z.string().nullable().default(null),
  sync_id: z.string().nullable().default(null),
  upgrade_id: z.string().nullable().default(null),
  action: z.enum(["created", "updated", "deleted"]),
});
export type CatalogChanged = z.infer<typeof CatalogChangedSchema>;

/** Событие ленты пользователя из шины (socket.io `user_event`): те же поля, что у сообщений
 * RunListChanged, WorkflowChanged, ConnectionsChanged, CatalogChanged на сервере. */
export const UserEventSchema = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("run_list_changed"),
    run_id: z.string(),
    workflow_id: z.string().uuid().nullable(),
    workflow_name: z.string(),
    status: z.string(),
  }),
  z.object({
    kind: z.literal("workflow_changed"),
    workflow_id: z.string().uuid(),
    name: z.string(),
    action: z.enum(["created", "updated", "deleted"]),
  }),
  z.object({
    kind: z.literal("workflow_draft_changed"),
    workflow_id: z.string().uuid(),
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
    connection_id: z.string().uuid(),
    name: z.string(),
    action: z.enum(["created", "updated", "deleted"]),
  }),
  z.object({
    kind: z.literal("signin_refresh_requested"),
    principal: z.string(),
  }),
  CatalogChangedSchema,
]);
export type UserEvent = z.infer<typeof UserEventSchema>;
