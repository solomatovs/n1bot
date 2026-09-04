import { z } from "zod";

/** Снимок каталога и записи сервиса, как их отдаёт JSON API; разбор на границе
 * делает zod, соответствие OpenAPI проверяет api/contract.ts на компиляции. */

export const LayerSchema = z.object({
  id: z.string(),
  name: z.string(),
});

export const DatasetSchema = z.object({
  id: z.string(),
  layer_id: z.string(),
  name: z.string(),
  source: z.string(),
  description: z.string(),
  tags: z.array(z.string()),
  owner: z.string(),
});

export const ColumnSchema = z.object({
  id: z.string(),
  dataset_id: z.string(),
  name: z.string(),
  type: z.string(),
  nullable: z.boolean(),
  is_key: z.boolean(),
  position: z.number(),
  description: z.string(),
});

export const LoadFieldTypeSchema = z.enum(["text", "int", "bool", "column", "columns"]);

export const LoadFieldSchema = z.object({
  name: z.string(),
  type: LoadFieldTypeSchema,
  required: z.boolean(),
  description: z.string(),
});

export const LoadKindSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  fields: z.array(LoadFieldSchema),
});

export const LoadValueSchema = z.union([z.string(), z.number(), z.boolean(), z.array(z.string())]);

export const LoadSpecSchema = z.object({
  kind_id: z.string(),
  values: z.record(LoadValueSchema),
});

export const FlowSchema = z.object({
  id: z.string(),
  from_dataset_id: z.string(),
  to_dataset_id: z.string(),
  load: LoadSpecSchema,
  description: z.string(),
});

export const SnapshotSchema = z.object({
  layers: z.record(LayerSchema),
  datasets: z.record(DatasetSchema),
  columns: z.record(ColumnSchema),
  load_kinds: z.record(LoadKindSchema),
  flows: z.record(FlowSchema),
});

export const EntityKindSchema = z.enum(["layer", "dataset", "column", "load_kind", "flow"]);
export const ChangeStatusSchema = z.enum(["added", "removed", "modified", "unchanged"]);

export const DiffEntrySchema = z.object({
  ref: z.object({ kind: EntityKindSchema, id: z.string() }),
  status: ChangeStatusSchema,
});

export const DiffSchema = z.object({ entries: z.array(DiffEntrySchema) });

export const DraftSchema = z.object({
  id: z.string(),
  name: z.string(),
  base_version: z.number(),
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

export const ViewSchema = z.object({
  id: z.string(),
  name: z.string(),
  owner_id: z.string(),
  dataset_ids: z.array(z.string()),
  layer_ids: z.array(z.string()),
  created_at: z.string(),
});

export const NodePositionSchema = z.object({
  dataset_id: z.string(),
  x: z.number(),
  y: z.number(),
});

export const ViewLayoutSchema = z.object({
  view_id: z.string(),
  positions: z.array(NodePositionSchema),
});

export const ViewStateSchema = z.object({
  view: ViewSchema,
  version: z.number(),
  snapshot: SnapshotSchema,
  layout: ViewLayoutSchema,
  owned: z.boolean(),
});

export const AccessSchema = z.object({
  user_id: z.string(),
  login: z.string(),
  can_view: z.boolean(),
  can_edit: z.boolean(),
});

export const ShareSchema = z.object({
  kind: z.enum(["role", "user"]),
  target: z.string(),
  mode: z.literal("view"),
});

export const VersionSchema = z.object({
  number: z.number(),
  author: z.object({ user_id: z.string(), via: z.enum(["user", "llm"]) }),
  published_at: z.string(),
});

export const RebaseIssueSchema = z.object({ seq: z.number(), index: z.number(), reason: z.string() });

export const RebaseResultSchema = z.object({
  draft: DraftSchema,
  issues: z.array(RebaseIssueSchema),
});

export const CatalogChangedSchema = z.object({
  kind: z.literal("catalog_changed"),
  draft_id: z.string().nullable(),
  version: z.number().nullable(),
  view_id: z.string().nullable(),
  source_id: z.string().nullable().default(null),
  action: z.enum(["created", "updated", "deleted"]),
});

export type Layer = z.infer<typeof LayerSchema>;
export type Dataset = z.infer<typeof DatasetSchema>;
export type Column = z.infer<typeof ColumnSchema>;
export type LoadField = z.infer<typeof LoadFieldSchema>;
export type LoadKind = z.infer<typeof LoadKindSchema>;
export type LoadValue = z.infer<typeof LoadValueSchema>;
export type Flow = z.infer<typeof FlowSchema>;
export type Snapshot = z.infer<typeof SnapshotSchema>;
export type EntityKind = z.infer<typeof EntityKindSchema>;
export type ChangeStatus = z.infer<typeof ChangeStatusSchema>;
export type Diff = z.infer<typeof DiffSchema>;
export type Draft = z.infer<typeof DraftSchema>;
export type DraftState = z.infer<typeof DraftStateSchema>;
export type View = z.infer<typeof ViewSchema>;
export type NodePosition = z.infer<typeof NodePositionSchema>;
export type ViewLayout = z.infer<typeof ViewLayoutSchema>;
export type ViewState = z.infer<typeof ViewStateSchema>;
export type Access = z.infer<typeof AccessSchema>;
export type Share = z.infer<typeof ShareSchema>;
/** Фильтр вида: пустые списки — весь каталог. */
export type ViewSpec = { name: string; dataset_ids: string[]; layer_ids: string[] };
export type Version = z.infer<typeof VersionSchema>;
export type RebaseIssue = z.infer<typeof RebaseIssueSchema>;
export type RebaseResult = z.infer<typeof RebaseResultSchema>;
export type CatalogChanged = z.infer<typeof CatalogChangedSchema>;

export const EMPTY_DIFF: Diff = { entries: [] };

/** Потоки набора: входящие и исходящие. */
export type DatasetFlows = {
  incoming: Flow[];
  outgoing: Flow[];
};

/** Значение поля загрузки, готовое к показу: ссылки на колонки — именами. */
export type LoadValueView = {
  field: string;
  text: string;
};

/** Снимок с индексами: слои по порядку, колонки набора, потоки набора, статусы diff. */
export class Catalog {
  private readonly statuses = new Map<string, ChangeStatus>();

  constructor(
    readonly snapshot: Snapshot,
    readonly diff: Diff = EMPTY_DIFF,
  ) {
    for (const entry of diff.entries) {
      this.statuses.set(`${entry.ref.kind}:${entry.ref.id}`, entry.status);
    }
  }

  get layers(): Layer[] {
    return Object.values(this.snapshot.layers);
  }

  get datasets(): Dataset[] {
    return Object.values(this.snapshot.datasets);
  }

  get flows(): Flow[] {
    return Object.values(this.snapshot.flows);
  }

  get loadKinds(): LoadKind[] {
    return Object.values(this.snapshot.load_kinds);
  }

  layer(id: string): Layer | undefined {
    return this.snapshot.layers[id];
  }

  dataset(id: string): Dataset | undefined {
    return this.snapshot.datasets[id];
  }

  column(id: string): Column | undefined {
    return this.snapshot.columns[id];
  }

  loadKind(id: string): LoadKind | undefined {
    return this.snapshot.load_kinds[id];
  }

  /** Номер слоя в порядке создания: партиция раскладки. */
  layerIndex(layerId: string): number {
    const index = this.layers.findIndex((layer) => layer.id === layerId);
    return index < 0 ? 0 : index;
  }

  datasetsOf(layerId: string): Dataset[] {
    return this.datasets.filter((dataset) => dataset.layer_id === layerId);
  }

  columnsOf(datasetId: string): Column[] {
    return Object.values(this.snapshot.columns)
      .filter((column) => column.dataset_id === datasetId)
      .sort((a, b) => a.position - b.position);
  }

  flowsOf(datasetId: string): DatasetFlows {
    const incoming: Flow[] = [];
    const outgoing: Flow[] = [];
    for (const flow of this.flows) {
      if (flow.to_dataset_id === datasetId) {
        incoming.push(flow);
      }
      if (flow.from_dataset_id === datasetId) {
        outgoing.push(flow);
      }
    }

    return { incoming, outgoing };
  }

  /** Соседи по потокам: наборы на другом конце. */
  neighbours(datasetId: string): Dataset[] {
    const ids = new Set<string>();
    for (const flow of this.flows) {
      if (flow.from_dataset_id === datasetId) {
        ids.add(flow.to_dataset_id);
      }
      if (flow.to_dataset_id === datasetId) {
        ids.add(flow.from_dataset_id);
      }
    }

    const found: Dataset[] = [];
    for (const id of ids) {
      const dataset = this.dataset(id);
      if (dataset !== undefined) {
        found.push(dataset);
      }
    }

    return found;
  }

  statusOf(kind: EntityKind, id: string): ChangeStatus {
    return this.statuses.get(`${kind}:${id}`) ?? "unchanged";
  }

  /** Имя вида загрузки потока; неизвестный вид — его id. */
  loadKindName(flow: Flow): string {
    return this.loadKind(flow.load.kind_id)?.name ?? flow.load.kind_id;
  }

  /** Значения правила загрузки для показа: колонки по именам, списки через запятую. */
  loadValues(flow: Flow): LoadValueView[] {
    const kind = this.loadKind(flow.load.kind_id);
    const views: LoadValueView[] = [];
    for (const [field, value] of Object.entries(flow.load.values)) {
      const type = kind?.fields.find((item) => item.name === field)?.type;
      views.push({ field, text: this.loadValueText(value, type) });
    }

    return views;
  }

  private loadValueText(value: LoadValue, type: LoadField["type"] | undefined): string {
    if (Array.isArray(value)) {
      return value.map((id) => this.column(id)?.name ?? id).join(", ");
    }

    if (type === "column" && typeof value === "string") {
      return this.column(value)?.name ?? value;
    }

    return String(value);
  }
}

// --- источники метаданных ---

export const SourceKindSchema = z.enum(["postgres", "clickhouse"]);
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
  source_id: z.string(),
  kind: ObjectKindSchema,
  path: z.array(z.string()),
});

export const SourceSchema = z.object({
  id: z.string(),
  kind: SourceKindSchema,
  name: z.string(),
  description: z.string(),
  manual: z.boolean(),
  created_by: z.string(),
  created_at: z.string(),
  latest_version: z.number(),
});

export const SourceVersionSchema = z.object({
  source_id: z.string(),
  version: z.number(),
  taken_at: z.string(),
  taken_by: z.string(),
  connection_id: z.string().nullable(),
  sync_id: z.string().nullable(),
  objects_total: z.number(),
  server_version: z.string().nullable(),
});

export const SourceConnectionSchema = z.object({
  source_id: z.string(),
  connection_id: z.string(),
  bound_by: z.string(),
  bound_at: z.string(),
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

export const SourceDraftSchema = z.object({
  id: z.string(),
  source_id: z.string(),
  name: z.string(),
  base_version: z.number(),
  status: z.enum(["open", "published", "discarded"]),
  created_by: z.string(),
  created_at: z.string(),
});

export const SourceDraftStateSchema = z.object({
  draft: SourceDraftSchema,
  snapshot: z.object({ kind: SourceKindSchema }).passthrough(),
  diff: SourceDiffSchema,
  seq: z.number(),
});

export type SourceKind = z.infer<typeof SourceKindSchema>;
export type ObjectKind = z.infer<typeof ObjectKindSchema>;
export type ObjectRef = z.infer<typeof ObjectRefSchema>;
export type Source = z.infer<typeof SourceSchema>;
export type SourceVersion = z.infer<typeof SourceVersionSchema>;
export type SourceConnection = z.infer<typeof SourceConnectionSchema>;
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
export type SourceDraft = z.infer<typeof SourceDraftSchema>;
export type SourceDraftState = z.infer<typeof SourceDraftStateSchema>;
/** Что задаёт пользователь, заводя источник. */
export type SourceSpec = { kind: SourceKind; name: string; description: string; manual: boolean };
/** Объект ручного источника коротким набором полей. */
export type ManualColumn = { name: string; type: string; nullable: boolean; comment: string | null };
export type ManualObject = {
  kind: "table" | "view";
  path: string[];
  comment: string | null;
  columns: ManualColumn[];
};
export type SourceOp =
  | { op: "add_object"; object: ManualObject }
  | { op: "set_object"; object: ManualObject }
  | { op: "remove_object"; path: string[] };
