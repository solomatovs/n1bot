import { z } from "zod";

import type { PageUrls } from "../config";
import {
  AccessSchema,
  ConnectionViewSchema,
  ProbeResultSchema,
  ObjectCardSchema,
  SourceConnectionSchema,
  SourceDiffSchema,
  SourceSchema,
  SourceVersionSchema,
  SyncSchema,
  TreeNodeSchema,
  CatalogChangedSchema,
  DraftSchema,
  DraftStateSchema,
  PinBumpSchema,
  ProcessContextSchema,
  RebaseResultSchema,
  ShareSchema,
  SnapshotSchema,
  VersionSchema,
  ViewLayoutSchema,
  ViewSchema,
  ViewStateSchema,
  type Access,
  type CatalogChanged,
  type ConnectionBody,
  type ConnectionView,
  type ProbeResult,
  type ObjectCard,
  type ObjectKind,
  type Source,
  type SourceConnection,
  type SourceDiff,
  type SourceCreate,
  type SourceSpec,
  type SourceVersion,
  type Sync,
  type SyncScope,
  type TreeNode,
  type Draft,
  type DraftState,
  type NodePosition,
  type PinBump,
  type ProcessContext,
  type RebaseResult,
  type Share,
  type Snapshot,
  type Version,
  type View,
  type ViewLayout,
  type ViewSpec,
  type ViewState,
} from "../model/catalog";
import type { CatalogOp } from "../model/ops";
import type { paths } from "./schema";

/** Отказ API: статус и текст detail; тело 409/422 сохраняется целиком. */
/** Ошибка поля из ответа 422 FastAPI: путь без «body» и текст. */
export type FieldIssue = { loc: (string | number)[]; message: string };

const ValidationErrorSchema = z.array(z.object({ loc: z.array(z.union([z.string(), z.number()])), msg: z.string() }));

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly payload: unknown,
    readonly issues: FieldIssue[] = [],
  ) {
    super(`${status}: ${detail}`);
  }

  static of(status: number, payload: unknown): ApiError {
    const detail = detailOf(payload);
    if (typeof detail === "string") {
      return new ApiError(status, detail, payload);
    }

    const validation = ValidationErrorSchema.safeParse(detail);
    if (validation.success) {
      const issues: FieldIssue[] = validation.data.map((item) => ({
        loc: item.loc.filter((part) => part !== "body"),
        message: item.msg,
      }));
      const text = issues.map((issue) => `${issue.loc.join(".")}: ${issue.message}`).join("\n");
      return new ApiError(status, text, payload, issues);
    }

    if (typeof detail === "object" && detail !== null && "message" in detail) {
      const message: unknown = detail.message;
      if (typeof message === "string") {
        return new ApiError(status, message, payload);
      }
    }

    return new ApiError(status, JSON.stringify(detail), payload);
  }
}

function detailOf(payload: unknown): unknown {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    return payload.detail;
  }

  return payload;
}

type Method = "get" | "post" | "put" | "delete";

const DeletedSchema = z.object({ deleted: z.boolean() });

/** Строка запроса дерева: path повторяется на каждую ступень. */
function treeQuery(path: string[], extra: Record<string, string>): string {
  const query = new URLSearchParams(extra);
  for (const step of path) {
    query.append("path", step);
  }

  return query.toString();
}

/** Пути API из OpenAPI: страница зовёт только их, параметры подставляются в шаблон. */
export type ApiPath = keyof paths;

/** Метка своего запроса — как OwnRequest на сервере. */
export const OWN_REQUEST = { header: "x-boba-request", value: "1" } as const;

/** Клиент JSON API каталога: пути из OpenAPI, ответы разбирает zod. */
export class CatalogApi {
  private unauthorized: (() => void) | null = null;

  constructor(private readonly urls: PageUrls) {}

  onUnauthorized(handler: (() => void) | null): void {
    this.unauthorized = handler;
  }

  snapshot(): Promise<Snapshot> {
    return this.call("get", "/api/catalog/snapshot", undefined, SnapshotSchema);
  }

  versions(): Promise<Version[]> {
    return this.call("get", "/api/catalog/versions", undefined, z.array(VersionSchema));
  }

  drafts(): Promise<Draft[]> {
    return this.call("get", "/api/catalog/drafts", undefined, z.array(DraftSchema));
  }

  draft(draftId: string): Promise<DraftState> {
    return this.call("get", `/api/catalog/drafts/${draftId}`, undefined, DraftStateSchema);
  }

  views(): Promise<View[]> {
    return this.call("get", "/api/catalog/views", undefined, z.array(ViewSchema));
  }

  view(viewId: string): Promise<View> {
    return this.call("get", `/api/catalog/views/${viewId}`, undefined, ViewSchema);
  }

  layout(viewId: string): Promise<ViewLayout> {
    return this.call("get", `/api/catalog/views/${viewId}/layout`, undefined, ViewLayoutSchema);
  }

  access(): Promise<Access> {
    return this.call("get", "/api/catalog/access", undefined, AccessSchema);
  }

  /** Страница вида одним ответом: срез снимка доступен и без ролей на каталог. */
  viewState(viewId: string): Promise<ViewState> {
    return this.call("get", `/api/catalog/views/${viewId}/state`, undefined, ViewStateSchema);
  }

  createView(spec: ViewSpec): Promise<View> {
    return this.call("post", "/api/catalog/views", spec, ViewSchema);
  }

  updateView(viewId: string, spec: ViewSpec): Promise<View> {
    return this.call("put", `/api/catalog/views/${viewId}`, spec, ViewSchema);
  }

  deleteView(viewId: string): Promise<void> {
    return this.call("delete", `/api/catalog/views/${viewId}`, undefined, DeletedSchema).then(() => undefined);
  }

  /** Полная замена раскладки вида. */
  putLayout(viewId: string, positions: NodePosition[]): Promise<ViewLayout> {
    return this.call("put", `/api/catalog/views/${viewId}/layout`, { positions }, ViewLayoutSchema);
  }

  shares(viewId: string): Promise<Share[]> {
    return this.call("get", `/api/catalog/views/${viewId}/shares`, undefined, z.array(ShareSchema));
  }

  share(viewId: string, share: Share): Promise<void> {
    return this.call("post", `/api/catalog/views/${viewId}/shares`, share, z.null()).then(() => undefined);
  }

  unshare(viewId: string, share: Share): Promise<void> {
    const path = `/api/catalog/views/${viewId}/shares/${share.kind}/${encodeURIComponent(share.target)}`;
    return this.call("delete", path, undefined, DeletedSchema).then(() => undefined);
  }

  createDraft(name: string): Promise<Draft> {
    return this.call("post", "/api/catalog/drafts", { name }, DraftSchema);
  }

  discardDraft(draftId: string): Promise<Draft> {
    return this.call("delete", `/api/catalog/drafts/${draftId}`, undefined, DraftSchema);
  }

  /** Порция операций; 409 с current_seq в payload — черновик ушёл вперёд. */
  appendOps(draftId: string, expectedSeq: number, operations: CatalogOp[]): Promise<DraftState> {
    const body = { expected_seq: expectedSeq, operations };
    return this.call("post", `/api/catalog/drafts/${draftId}/ops`, body, DraftStateSchema);
  }

  publish(draftId: string): Promise<Version> {
    return this.call("post", `/api/catalog/drafts/${draftId}/publish`, undefined, VersionSchema);
  }

  rebase(draftId: string, dropConflicts: boolean): Promise<RebaseResult> {
    const body = { drop_conflicts: dropConflicts };
    return this.call("post", `/api/catalog/drafts/${draftId}/rebase`, body, RebaseResultSchema);
  }

  /** Привязки черновика поднимаются до последних версий источников. */
  bumpPins(draftId: string): Promise<PinBump> {
    return this.call("post", `/api/catalog/drafts/${draftId}/pins`, undefined, PinBumpSchema);
  }

  // --- контекст процесса: колонки узлов и устаревание по привязкам ---

  context(): Promise<ProcessContext> {
    return this.call("get", "/api/catalog/context", undefined, ProcessContextSchema);
  }

  draftContext(draftId: string): Promise<ProcessContext> {
    return this.call("get", `/api/catalog/drafts/${draftId}/context`, undefined, ProcessContextSchema);
  }

  viewContext(viewId: string): Promise<ProcessContext> {
    return this.call("get", `/api/catalog/views/${viewId}/context`, undefined, ProcessContextSchema);
  }

  /** Карточка объекта узла из среза вида: доступна тем, кому вид расшарен. */
  viewObject(viewId: string, nodeId: string): Promise<ObjectCard> {
    return this.call("get", `/api/catalog/views/${viewId}/nodes/${nodeId}/object`, undefined, ObjectCardSchema);
  }

  // --- источники ---

  sourceKinds(): Promise<string[]> {
    return this.call("get", "/api/catalog/source-kinds", undefined, z.array(z.string()));
  }

  sources(): Promise<Source[]> {
    return this.call("get", "/api/catalog/sources", undefined, z.array(SourceSchema));
  }

  source(sourceId: string): Promise<Source> {
    return this.call("get", `/api/catalog/sources/${sourceId}`, undefined, SourceSchema);
  }

  createSource(spec: SourceCreate): Promise<Source> {
    return this.call("post", "/api/catalog/sources", spec, SourceSchema);
  }

  updateSource(sourceId: string, spec: SourceSpec): Promise<Source> {
    return this.call("put", `/api/catalog/sources/${sourceId}`, spec, SourceSchema);
  }

  deleteSource(sourceId: string): Promise<void> {
    return this.call("delete", `/api/catalog/sources/${sourceId}`, undefined, DeletedSchema).then(() => undefined);
  }

  sourceConnections(sourceId: string): Promise<SourceConnection[]> {
    const path = `/api/catalog/sources/${sourceId}/connections`;
    return this.call("get", path, undefined, z.array(SourceConnectionSchema));
  }

  bindConnection(sourceId: string, connectionId: string): Promise<SourceConnection> {
    const path = `/api/catalog/sources/${sourceId}/connections`;
    return this.call("post", path, { connection_id: connectionId }, SourceConnectionSchema);
  }

  unbindConnection(sourceId: string, connectionId: string): Promise<void> {
    const path = `/api/catalog/sources/${sourceId}/connections/${connectionId}`;
    return this.call("delete", path, undefined, DeletedSchema).then(() => undefined);
  }

  /** Подключения пользователя: общий API брокера под префиксом каталога. */
  connections(kind?: string): Promise<ConnectionView[]> {
    const query = kind === undefined ? "" : `?${new URLSearchParams({ kind }).toString()}`;
    return this.call("get", `/api/catalog/connections${query}`, undefined, z.array(ConnectionViewSchema));
  }

  connectionSchema(): Promise<unknown> {
    return this.call("get", "/api/catalog/connections/schema", undefined, z.unknown());
  }

  createConnection(body: ConnectionBody): Promise<ConnectionView> {
    return this.call("post", "/api/catalog/connections", body, ConnectionViewSchema);
  }

  replaceConnection(connectionId: string, body: ConnectionBody): Promise<ConnectionView> {
    return this.call("put", `/api/catalog/connections/${connectionId}`, body, ConnectionViewSchema);
  }

  removeConnection(connectionId: string): Promise<void> {
    return this.call("delete", `/api/catalog/connections/${connectionId}`, undefined, DeletedSchema).then(
      () => undefined,
    );
  }

  checkConnection(profile: Record<string, unknown>): Promise<ProbeResult> {
    return this.call("post", "/api/catalog/connections/check", { profile }, ProbeResultSchema);
  }

  checkStoredConnection(connectionId: string): Promise<ProbeResult> {
    return this.call("post", `/api/catalog/connections/${connectionId}/check`, undefined, ProbeResultSchema);
  }

  sourceSyncs(sourceId: string): Promise<Sync[]> {
    return this.call("get", `/api/catalog/sources/${sourceId}/syncs`, undefined, z.array(SyncSchema));
  }

  startSync(sourceId: string, connectionId: string, scope: SyncScope): Promise<Sync> {
    const body = { connection_id: connectionId, scope };
    return this.call("post", `/api/catalog/sources/${sourceId}/syncs`, body, SyncSchema);
  }

  sync(syncId: string): Promise<Sync> {
    return this.call("get", `/api/catalog/syncs/${syncId}`, undefined, SyncSchema);
  }

  cancelSync(syncId: string): Promise<Sync> {
    return this.call("delete", `/api/catalog/syncs/${syncId}`, undefined, SyncSchema);
  }

  sourceVersions(sourceId: string): Promise<SourceVersion[]> {
    const path = `/api/catalog/sources/${sourceId}/versions`;
    return this.call("get", path, undefined, z.array(SourceVersionSchema));
  }

  /** Дети узла дерева версии; version < 0 — последняя. */
  sourceTree(sourceId: string, version: number, path: string[]): Promise<TreeNode[]> {
    const query = treeQuery(path, { version: String(version) });
    return this.call("get", `/api/catalog/sources/${sourceId}/tree?${query}`, undefined, z.array(TreeNodeSchema));
  }

  sourceObject(sourceId: string, version: number, kind: ObjectKind, path: string[]): Promise<ObjectCard> {
    const query = treeQuery(path, { version: String(version), kind });
    return this.call("get", `/api/catalog/sources/${sourceId}/object?${query}`, undefined, ObjectCardSchema);
  }

  sourceDiff(sourceId: string, oldVersion: number, newVersion: number): Promise<SourceDiff> {
    const query = new URLSearchParams({ old: String(oldVersion), new: String(newVersion) });
    return this.call("get", `/api/catalog/sources/${sourceId}/diff?${query.toString()}`, undefined, SourceDiffSchema);
  }

  /** Поток CatalogChanged пользователя: server-sent events с cookie входа. */
  events(onMessage: (message: CatalogChanged) => void): () => void {
    const source = new EventSource(this.urls.api("/events"), { withCredentials: true });
    source.onmessage = (event: MessageEvent<string>) => {
      const parsed = CatalogChangedSchema.safeParse(JSON.parse(event.data));
      if (parsed.success) {
        onMessage(parsed.data);
      }
    };

    return () => {
      source.close();
    };
  }

  private async call<T>(method: Method, path: string, body: unknown, schema: z.ZodType<T>): Promise<T> {
    const headers: Record<string, string> = { [OWN_REQUEST.header]: OWN_REQUEST.value };
    const init: RequestInit = { method: method.toUpperCase(), headers, credentials: "same-origin" };
    if (body !== undefined) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    const response = await fetch(this.urls.api(path.replace(/^\/api\/catalog/, "")), init);

    if (response.status === 401) {
      this.unauthorized?.();
    }

    const text = await response.text();
    const parsed: unknown = text === "" ? null : JSON.parse(text);
    if (!response.ok) {
      throw ApiError.of(response.status, parsed);
    }

    return schema.parse(parsed);
  }
}
