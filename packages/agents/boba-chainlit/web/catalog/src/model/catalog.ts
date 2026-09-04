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
