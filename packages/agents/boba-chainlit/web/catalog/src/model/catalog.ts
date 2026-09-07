import { z } from "zod";

/** Снимок процесса, записи сервиса и снимки подключений, как их отдаёт JSON
 * API; разбор на границе делает zod, соответствие OpenAPI проверяет
 * api/contract.ts на компиляции. */

// --- снимки подключений: адреса объектов ---

export const SourceKindSchema = z.string().min(1);
export const ObjectKindSchema = z.enum([
  "database",
  "schema",
  "relation",
  "routine",
  "sequence",
  "type",
  "table",
  "dictionary",
]);

export const ObjectRefSchema = z.object({
  connection_id: z.string(),
  kind: ObjectKindSchema,
  path: z.array(z.string()),
});

export type SourceKind = z.infer<typeof SourceKindSchema>;
export type ObjectKind = z.infer<typeof ObjectKindSchema>;
export type ObjectRef = z.infer<typeof ObjectRefSchema>;

/** Адрес объекта строкой: путь через «/», как его печатает сервер в подписях. */
export function renderRef(ref: ObjectRef): string {
  return ref.path.join("/");
}

export function sameRef(a: ObjectRef, b: ObjectRef): boolean {
  return a.connection_id === b.connection_id && a.kind === b.kind && renderRef(a) === renderRef(b);
}

// --- процесс: группы, узлы, потоки ---

export const GroupSchema = z.object({
  id: z.string(),
  name: z.string(),
});

export const PositionSchema = z.object({
  x: z.number(),
  y: z.number(),
});

export const NodeSchema = z.object({
  id: z.string(),
  ref: ObjectRefSchema,
  position: PositionSchema.nullable(),
  group_id: z.string().nullable(),
  alias: z.string().nullable(),
  note: z.string(),
});

export const ColumnLinkSchema = z.object({
  from_column: z.string(),
  to_column: z.string(),
});

export const FlowSchema = z.object({
  id: z.string(),
  from_node_id: z.string(),
  to_node_id: z.string(),
  columns: z.array(ColumnLinkSchema),
  description: z.string(),
});

export const SnapshotSchema = z.object({
  groups: z.record(GroupSchema),
  nodes: z.record(NodeSchema),
  flows: z.record(FlowSchema),
});

export const EntityKindSchema = z.enum(["group", "node", "flow"]);
export const ChangeStatusSchema = z.enum(["added", "removed", "modified", "unchanged"]);

export const EntityRefSchema = z.object({ kind: EntityKindSchema, id: z.string() });

export const DiffEntrySchema = z.object({
  ref: EntityRefSchema,
  status: ChangeStatusSchema,
});

export const DiffSchema = z.object({ entries: z.array(DiffEntrySchema) });

const PinsSchema = z.record(z.number());

export const ProcessSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  owner_id: z.string(),
  created_at: z.string(),
  latest_version: z.number(),
  nodes: z.number(),
  open_drafts: z.number(),
});

export const DraftSchema = z.object({
  id: z.string(),
  process_id: z.string().nullable(),
  name: z.string(),
  base_version: z.number(),
  pins: PinsSchema,
  status: z.enum(["open", "published", "discarded"]),
  created_by: z.string(),
  created_at: z.string(),
});

export const DraftStateSchema = z.object({
  draft: DraftSchema,
  snapshot: SnapshotSchema,
  diff: DiffSchema,
  seq: z.number(),
});

export const AccessSchema = z.object({
  user_id: z.string(),
  login: z.string(),
  can_view: z.boolean(),
  can_edit: z.boolean(),
});

export const ShareSchema = z.object({
  token: z.string(),
  process_id: z.string(),
  created_by: z.string(),
  created_at: z.string(),
  revoked_at: z.string().nullable(),
});

export const VersionSchema = z.object({
  process_id: z.string(),
  number: z.number(),
  pins: PinsSchema,
  author: z.object({ user_id: z.string(), via: z.enum(["user", "llm"]) }),
  published_at: z.string(),
});

export const RebaseIssueSchema = z.object({ seq: z.number(), index: z.number(), reason: z.string() });

export const RebaseResultSchema = z.object({
  draft: DraftSchema,
  issues: z.array(RebaseIssueSchema),
});

export const StaleReasonSchema = z.enum(["object_removed", "object_changed", "column_removed", "column_changed"]);

export const StaleSchema = z.object({
  target: EntityRefSchema,
  connection_id: z.string(),
  pinned_version: z.number(),
  since_version: z.number(),
  reason: StaleReasonSchema,
  detail: z.record(z.string()),
});

export const StalenessSchema = z.object({ entries: z.array(StaleSchema) });

export const NodeColumnSchema = z.object({
  name: z.string(),
  type: z.string(),
  nullable: z.boolean(),
  key: z.boolean(),
});

export const ProcessContextSchema = z.object({
  pins: PinsSchema,
  columns: z.record(z.array(NodeColumnSchema)),
  stale: StalenessSchema,
});

export const PinBumpSchema = z.object({
  draft: DraftSchema,
  violations: z.array(z.string()),
});

export const SharedProcessSchema = z.object({
  process: ProcessSchema,
  snapshot: SnapshotSchema,
  context: ProcessContextSchema,
});

export const CatalogChangedSchema = z.object({
  kind: z.literal("catalog_changed"),
  process_id: z.string().nullable().default(null),
  version: z.number().nullable().default(null),
  draft_id: z.string().nullable().default(null),
  connection_id: z.string().nullable().default(null),
  sync_id: z.string().nullable().default(null),
  action: z.enum(["created", "updated", "deleted"]),
});

export type Group = z.infer<typeof GroupSchema>;
export type Position = z.infer<typeof PositionSchema>;
export type ProcessNode = z.infer<typeof NodeSchema>;
export type ColumnLink = z.infer<typeof ColumnLinkSchema>;
export type Flow = z.infer<typeof FlowSchema>;
export type Snapshot = z.infer<typeof SnapshotSchema>;
export type EntityKind = z.infer<typeof EntityKindSchema>;
export type EntityRef = z.infer<typeof EntityRefSchema>;
export type ChangeStatus = z.infer<typeof ChangeStatusSchema>;
export type Diff = z.infer<typeof DiffSchema>;
export type Process = z.infer<typeof ProcessSchema>;
/** Что задаёт пользователь, заводя или правя процесс. */
export type ProcessSpec = { name: string; description: string };
export type Draft = z.infer<typeof DraftSchema>;
export type DraftState = z.infer<typeof DraftStateSchema>;
export type Access = z.infer<typeof AccessSchema>;
export type Share = z.infer<typeof ShareSchema>;
export type SharedProcess = z.infer<typeof SharedProcessSchema>;
export type Version = z.infer<typeof VersionSchema>;
export type RebaseIssue = z.infer<typeof RebaseIssueSchema>;
export type RebaseResult = z.infer<typeof RebaseResultSchema>;
export type Stale = z.infer<typeof StaleSchema>;
export type StaleReason = z.infer<typeof StaleReasonSchema>;
export type Staleness = z.infer<typeof StalenessSchema>;
export type NodeColumn = z.infer<typeof NodeColumnSchema>;
export type ProcessContext = z.infer<typeof ProcessContextSchema>;
export type PinBump = z.infer<typeof PinBumpSchema>;
export type CatalogChanged = z.infer<typeof CatalogChangedSchema>;

export const EMPTY_DIFF: Diff = { entries: [] };
export const EMPTY_CONTEXT: ProcessContext = { pins: {}, columns: {}, stale: { entries: [] } };

/** Потоки узла: входящие и исходящие. */
export type NodeFlows = {
  incoming: Flow[];
  outgoing: Flow[];
};

/** Подпись узла: alias, если задан, иначе последняя ступень адреса. */
export function nodeLabel(node: ProcessNode): string {
  if (node.alias !== null && node.alias !== "") {
    return node.alias;
  }

  return node.ref.path.at(-1) ?? renderRef(node.ref);
}

/** Подпись ребра: сколько колонок переходит; без пар — стрелка. */
export function flowLabel(flow: Flow): string {
  if (flow.columns.length === 0) {
    return "→";
  }

  return `${flow.columns.length} col${flow.columns.length === 1 ? "" : "s"}`;
}

/** Снимок процесса с индексами и контекстом снимков: слои по порядку, узлы
 * слоя, потоки узла, колонки из привязанной версии, статусы diff и устаревание. */
export class Catalog {
  private readonly statuses = new Map<string, ChangeStatus>();
  private readonly stales = new Map<string, Stale[]>();

  constructor(
    readonly snapshot: Snapshot,
    readonly diff: Diff = EMPTY_DIFF,
    readonly context: ProcessContext = EMPTY_CONTEXT,
  ) {
    for (const entry of diff.entries) {
      this.statuses.set(`${entry.ref.kind}:${entry.ref.id}`, entry.status);
    }

    for (const stale of context.stale.entries) {
      const key = `${stale.target.kind}:${stale.target.id}`;
      const list = this.stales.get(key) ?? [];
      list.push(stale);
      this.stales.set(key, list);
    }
  }

  get groups(): Group[] {
    return Object.values(this.snapshot.groups).sort((a, b) => a.name.localeCompare(b.name));
  }

  get nodes(): ProcessNode[] {
    return Object.values(this.snapshot.nodes);
  }

  get flows(): Flow[] {
    return Object.values(this.snapshot.flows);
  }

  /** Сколько узлов и потоков устарело относительно последних версий снимков. */
  get staleCount(): number {
    return this.stales.size;
  }

  group(id: string | null): Group | undefined {
    if (id === null) {
      return undefined;
    }

    return this.snapshot.groups[id];
  }

  node(id: string): ProcessNode | undefined {
    return this.snapshot.nodes[id];
  }

  /** Узел по адресу объекта: один объект стоит не больше чем в одном слое. */
  nodeOf(ref: ObjectRef): ProcessNode | undefined {
    return this.nodes.find((node) => sameRef(node.ref, ref));
  }

  /** Узлы группы. */
  nodesOf(groupId: string): ProcessNode[] {
    return this.nodes.filter((node) => node.group_id === groupId);
  }

  /** Поток из одной карточки в другую: между парой карточек он один. */
  flowBetween(from: string, to: string): Flow | undefined {
    return this.flows.find((flow) => flow.from_node_id === from && flow.to_node_id === to);
  }

  columnsOf(nodeId: string): NodeColumn[] {
    return this.context.columns[nodeId] ?? [];
  }

  flowsOf(nodeId: string): NodeFlows {
    const incoming: Flow[] = [];
    const outgoing: Flow[] = [];
    for (const flow of this.flows) {
      if (flow.to_node_id === nodeId) {
        incoming.push(flow);
      }
      if (flow.from_node_id === nodeId) {
        outgoing.push(flow);
      }
    }

    return { incoming, outgoing };
  }

  /** Соседи по потокам: узлы на другом конце. */
  neighbours(nodeId: string): ProcessNode[] {
    const ids = new Set<string>();
    for (const flow of this.flows) {
      if (flow.from_node_id === nodeId) {
        ids.add(flow.to_node_id);
      }
      if (flow.to_node_id === nodeId) {
        ids.add(flow.from_node_id);
      }
    }

    const found: ProcessNode[] = [];
    for (const id of ids) {
      const node = this.node(id);
      if (node !== undefined) {
        found.push(node);
      }
    }

    return found;
  }

  statusOf(kind: EntityKind, id: string): ChangeStatus {
    return this.statuses.get(`${kind}:${id}`) ?? "unchanged";
  }

  staleOf(kind: EntityKind, id: string): Stale[] {
    return this.stales.get(`${kind}:${id}`) ?? [];
  }

  /** Подпись узла по id; неизвестный узел — его id. */
  label(nodeId: string): string {
    const node = this.node(nodeId);
    return node === undefined ? nodeId : nodeLabel(node);
  }
}

// --- снимки подключений: записи, дерево, карточки, diff ---

export const SyncedConnectionSchema = z.object({
  connection_id: z.string(),
  name: z.string(),
  kind: SourceKindSchema,
  latest_version: z.number(),
  synced_at: z.string(),
});

export const ConnectionVersionSchema = z.object({
  connection_id: z.string(),
  version: z.number(),
  connection_name: z.string(),
  kind: SourceKindSchema,
  taken_at: z.string(),
  taken_by: z.string(),
  sync_id: z.string().nullable(),
  objects_total: z.number(),
  server_version: z.string().nullable(),
});

export const ConnectionViewSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: z.string(),
  mine: z.boolean(),
  available: z.boolean(),
  profile: z.record(z.unknown()).nullable(),
});

export const ProbeResultSchema = z.object({
  ok: z.boolean(),
  message: z.string(),
  elapsed_ms: z.number(),
});

export const SyncStatusSchema = z.enum(["running", "done", "failed", "cancelled"]);

export const SyncScopeSchema = z.object({
  schemas: z.array(z.string()),
  batch_size: z.number(),
  pause_ms: z.number(),
});

export const SyncSchema = z.object({
  id: z.string(),
  connection_id: z.string(),
  connection_name: z.string(),
  kind: SourceKindSchema,
  started_by: z.string(),
  started_at: z.string(),
  finished_at: z.string().nullable(),
  status: SyncStatusSchema,
  scope: SyncScopeSchema,
  objects_total: z.number().nullable(),
  objects_done: z.number(),
  error: z.string().nullable(),
  version: z.number().nullable(),
});

export const TreeNodeSchema = z.object({
  path: z.array(z.string()),
  label: z.string(),
  kind: z.enum(["database", "schema", "group", "object"]),
  children_count: z.number(),
  detail: z.string(),
  comment: z.string().nullable(),
  ref: ObjectRefSchema.nullable(),
  status: ChangeStatusSchema,
});

const nullableText = z.string().nullable();

export const PgRelationSchema = z.object({
  database: z.string(),
  schema_name: z.string(),
  name: z.string(),
  kind: z.enum(["table", "partitioned", "partition", "view", "materialized", "foreign"]),
  owner: z.string(),
  comment: nullableText,
  tablespace: nullableText,
  persistence: z.string(),
  row_estimate: z.number(),
  total_bytes: z.number(),
  partition_key: nullableText,
  partition_of: nullableText,
  partition_bound: nullableText,
  definition: nullableText,
  check_option: nullableText,
  populated: z.boolean().nullable(),
  foreign_server: nullableText,
  options: z.record(z.string(), z.string()),
});

export const PgColumnSchema = z.object({
  database: z.string(),
  schema_name: z.string(),
  relation: z.string(),
  name: z.string(),
  ordinal: z.number(),
  type: z.string(),
  nullable: z.boolean(),
  default: nullableText,
  identity: nullableText,
  generated: nullableText,
  collation: nullableText,
  comment: nullableText,
});

export const PgConstraintSchema = z.object({
  name: z.string(),
  kind: z.enum(["primary", "unique", "foreign", "check", "exclusion"]),
  columns: z.array(z.string()),
  ref_schema: nullableText,
  ref_relation: nullableText,
  ref_columns: z.array(z.string()).nullable(),
  on_update: nullableText,
  on_delete: nullableText,
  deferrable: z.boolean(),
  initially_deferred: z.boolean(),
  definition: z.string(),
  comment: nullableText,
});

export const PgIndexSchema = z.object({
  name: z.string(),
  method: z.string(),
  unique: z.boolean(),
  primary: z.boolean(),
  columns: z.array(z.string()),
  predicate: nullableText,
  definition: z.string(),
  total_bytes: z.number(),
  comment: nullableText,
});

export const PgRoutineSchema = z.object({
  database: z.string(),
  schema_name: z.string(),
  name: z.string(),
  signature: z.string(),
  kind: z.enum(["function", "procedure", "aggregate", "window"]),
  owner: z.string(),
  language: z.string(),
  arguments: z.string(),
  returns: nullableText,
  returns_set: z.boolean(),
  volatility: z.string(),
  strict: z.boolean(),
  security_definer: z.boolean(),
  parallel: z.string(),
  cost: z.number(),
  rows: z.number().nullable(),
  body: z.string(),
  definition: z.string(),
  comment: nullableText,
});

export const PgRoutineArgSchema = z.object({
  position: z.number(),
  name: nullableText,
  type: z.string(),
  mode: z.string(),
  default: nullableText,
});

export const PgSequenceSchema = z.object({
  database: z.string(),
  schema_name: z.string(),
  name: z.string(),
  type: z.string(),
  start: z.number(),
  minimum: z.number(),
  maximum: z.number(),
  increment: z.number(),
  cycle: z.boolean(),
  cache: z.number(),
  last_value: z.number().nullable(),
  owned_by: nullableText,
  comment: nullableText,
});

export const PgTypeSchema = z.object({
  database: z.string(),
  schema_name: z.string(),
  name: z.string(),
  kind: z.enum(["enum", "domain", "composite", "range"]),
  owner: z.string(),
  labels: z.array(z.string()).nullable(),
  base_type: nullableText,
  constraint: nullableText,
  attributes: z.array(z.object({ name: z.string(), type: z.string() })).nullable(),
  comment: nullableText,
});

export const ChTableSchema = z.object({
  database: z.string(),
  name: z.string(),
  kind: z.enum(["table", "view", "materialized", "live", "dictionary_table"]),
  engine: z.string(),
  engine_full: z.string(),
  comment: nullableText,
  partition_key: nullableText,
  sorting_key: nullableText,
  primary_key: nullableText,
  sampling_key: nullableText,
  ttl: nullableText,
  settings: z.record(z.string(), z.string()),
  definition: nullableText,
  target: nullableText,
  dependencies: z.array(z.string()),
  total_rows: z.number().nullable(),
  total_bytes: z.number().nullable(),
  metadata_modified_at: z.string(),
  create_query: z.string(),
});

export const ChColumnSchema = z.object({
  name: z.string(),
  position: z.number(),
  type: z.string(),
  default_kind: nullableText,
  default_expression: nullableText,
  comment: nullableText,
  codec: nullableText,
  ttl: nullableText,
  in_partition_key: z.boolean(),
  in_sorting_key: z.boolean(),
  in_primary_key: z.boolean(),
  in_sampling_key: z.boolean(),
});

export const ChDictionarySchema = z.object({
  database: z.string(),
  name: z.string(),
  status: z.string(),
  layout: z.string(),
  source: z.string(),
  key_columns: z.array(z.string()),
  lifetime_min: z.number(),
  lifetime_max: z.number(),
  comment: nullableText,
  create_query: z.string(),
});

export const ChDictionaryAttributeSchema = z.object({
  name: z.string(),
  position: z.number(),
  type: z.string(),
});

export const ObjectCardSchema = z.discriminatedUnion("card", [
  z.object({
    card: z.literal("pg_relation"),
    ref: ObjectRefSchema,
    relation: PgRelationSchema,
    columns: z.array(PgColumnSchema),
    constraints: z.array(PgConstraintSchema),
    indexes: z.array(PgIndexSchema),
    partitions: z.array(PgRelationSchema),
  }),
  z.object({
    card: z.literal("pg_routine"),
    ref: ObjectRefSchema,
    routine: PgRoutineSchema,
    arguments: z.array(PgRoutineArgSchema),
  }),
  z.object({ card: z.literal("pg_sequence"), ref: ObjectRefSchema, sequence: PgSequenceSchema }),
  z.object({ card: z.literal("pg_type"), ref: ObjectRefSchema, type: PgTypeSchema }),
  z.object({
    card: z.literal("ch_table"),
    ref: ObjectRefSchema,
    table: ChTableSchema,
    columns: z.array(ChColumnSchema),
  }),
  z.object({
    card: z.literal("ch_dictionary"),
    ref: ObjectRefSchema,
    dictionary: ChDictionarySchema,
    attributes: z.array(ChDictionaryAttributeSchema),
  }),
]);

export const FieldChangeSchema = z.object({
  field: z.string(),
  was: nullableText,
  now: nullableText,
});

export const PartChangeSchema = z.object({
  part: z.enum(["column", "constraint", "index", "argument", "attribute"]),
  name: z.string(),
  status: ChangeStatusSchema,
  fields: z.array(FieldChangeSchema),
});

export const ObjectChangeSchema = z.object({
  ref: ObjectRefSchema,
  label: z.string(),
  status: ChangeStatusSchema,
  fields: z.array(FieldChangeSchema),
  parts: z.array(PartChangeSchema),
});

export const SourceDiffSchema = z.object({ entries: z.array(ObjectChangeSchema) });

export type SyncedConnection = z.infer<typeof SyncedConnectionSchema>;
export type ConnectionVersion = z.infer<typeof ConnectionVersionSchema>;
export type ConnectionView = z.infer<typeof ConnectionViewSchema>;
export type ProbeResult = z.infer<typeof ProbeResultSchema>;
/** Имя и сырой профиль подключения по схеме api. */
export type ConnectionBody = { name: string; profile: Record<string, unknown> };
export type SyncStatus = z.infer<typeof SyncStatusSchema>;
export type SyncScope = z.infer<typeof SyncScopeSchema>;
export type Sync = z.infer<typeof SyncSchema>;
export type TreeNode = z.infer<typeof TreeNodeSchema>;
export type ObjectCard = z.infer<typeof ObjectCardSchema>;
export type PgRelation = z.infer<typeof PgRelationSchema>;
export type PgColumn = z.infer<typeof PgColumnSchema>;
export type ChTable = z.infer<typeof ChTableSchema>;
export type ChColumn = z.infer<typeof ChColumnSchema>;
export type FieldChange = z.infer<typeof FieldChangeSchema>;
export type PartChange = z.infer<typeof PartChangeSchema>;
export type ObjectChange = z.infer<typeof ObjectChangeSchema>;
export type SourceDiff = z.infer<typeof SourceDiffSchema>;

/** Подключение на странице: строка брокера и что о ней знает каталог. */
export type ConnectionRow = {
  view: ConnectionView;
  /** У вида подключения есть снимок: его можно синхронизировать. */
  syncable: boolean;
  synced: SyncedConnection | undefined;
};

/** Список подключений с пометками каталога; чужие личные подключения с
 * версиями в брокере не видны, но в каталоге есть: они идут без строки. */
export function connectionRows(
  views: ConnectionView[],
  kinds: string[],
  synced: SyncedConnection[],
): { rows: ConnectionRow[]; foreign: SyncedConnection[] } {
  const byId = new Map(synced.map((item) => [item.connection_id, item]));
  const rows = views.map((view) => ({
    view,
    syncable: kinds.includes(view.kind),
    synced: byId.get(view.id),
  }));
  const seen = new Set(views.map((view) => view.id));
  const foreign = synced.filter((item) => !seen.has(item.connection_id));
  return { rows, foreign };
}
