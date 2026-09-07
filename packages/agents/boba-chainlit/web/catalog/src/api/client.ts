import { z } from "zod";

import type { PageUrls } from "../config";
import {
  AccessSchema,
  CatalogChangedSchema,
  ConnectionVersionSchema,
  ConnectionViewSchema,
  DraftSchema,
  DraftStateSchema,
  ObjectCardSchema,
  PinBumpSchema,
  ProbeResultSchema,
  ProcessContextSchema,
  ProcessSchema,
  RebaseResultSchema,
  ShareSchema,
  SharedProcessSchema,
  SnapshotSchema,
  SourceDiffSchema,
  SyncSchema,
  SyncedConnectionSchema,
  TreeNodeSchema,
  VersionSchema,
  type Access,
  type CatalogChanged,
  type ConnectionBody,
  type ConnectionVersion,
  type ConnectionView,
  type Draft,
  type DraftState,
  type ObjectCard,
  type ObjectKind,
  type PinBump,
  type ProbeResult,
  type Process,
  type ProcessContext,
  type ProcessSpec,
  type RebaseResult,
  type Share,
  type SharedProcess,
  type Snapshot,
  type SourceDiff,
  type Sync,
  type SyncScope,
  type SyncedConnection,
  type TreeNode,
  type Version,
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
const ForgottenSchema = z.object({ versions: z.number() });

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

  access(): Promise<Access> {
    return this.call("get", "/api/catalog/access", undefined, AccessSchema);
  }

  // --- процессы ---

  processes(): Promise<Process[]> {
    return this.call("get", "/api/catalog/processes", undefined, z.array(ProcessSchema));
  }

  process(processId: string): Promise<Process> {
    return this.call("get", `/api/catalog/processes/${processId}`, undefined, ProcessSchema);
  }

  createProcess(spec: ProcessSpec): Promise<Process> {
    return this.call("post", "/api/catalog/processes", spec, ProcessSchema);
  }

  updateProcess(processId: string, spec: ProcessSpec): Promise<Process> {
    return this.call("put", `/api/catalog/processes/${processId}`, spec, ProcessSchema);
  }

  deleteProcess(processId: string): Promise<void> {
    return this.call("delete", `/api/catalog/processes/${processId}`, undefined, DeletedSchema).then(() => undefined);
  }

  snapshot(processId: string): Promise<Snapshot> {
    return this.call("get", `/api/catalog/processes/${processId}/snapshot`, undefined, SnapshotSchema);
  }

  versions(processId: string): Promise<Version[]> {
    return this.call("get", `/api/catalog/processes/${processId}/versions`, undefined, z.array(VersionSchema));
  }

  context(processId: string): Promise<ProcessContext> {
    return this.call("get", `/api/catalog/processes/${processId}/context`, undefined, ProcessContextSchema);
  }

  // --- черновики ---

  /** Свои открытые черновики по всем процессам. */
  myDrafts(): Promise<Draft[]> {
    return this.call("get", "/api/catalog/drafts", undefined, z.array(DraftSchema));
  }

  /** Черновик процесса; без процесса — черновик нового процесса. */
  createDraft(processId: string | null, name: string): Promise<Draft> {
    return this.call("post", "/api/catalog/drafts", { process_id: processId, name }, DraftSchema);
  }

  renameDraft(draftId: string, name: string): Promise<Draft> {
    return this.call("put", `/api/catalog/drafts/${draftId}`, { name }, DraftSchema);
  }

  draft(draftId: string): Promise<DraftState> {
    return this.call("get", `/api/catalog/drafts/${draftId}`, undefined, DraftStateSchema);
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

  /** Привязки черновика поднимаются до последних версий снимков. */
  bumpPins(draftId: string): Promise<PinBump> {
    return this.call("post", `/api/catalog/drafts/${draftId}/pins`, undefined, PinBumpSchema);
  }

  draftContext(draftId: string): Promise<ProcessContext> {
    return this.call("get", `/api/catalog/drafts/${draftId}/context`, undefined, ProcessContextSchema);
  }

  // --- ссылки на просмотр ---

  shares(processId: string): Promise<Share[]> {
    return this.call("get", `/api/catalog/processes/${processId}/shares`, undefined, z.array(ShareSchema));
  }

  share(processId: string): Promise<Share> {
    return this.call("post", `/api/catalog/processes/${processId}/shares`, undefined, ShareSchema);
  }

  revokeShare(token: string): Promise<Share> {
    return this.call("delete", `/api/catalog/shares/${encodeURIComponent(token)}`, undefined, ShareSchema);
  }

  /** Опубликованный процесс по ссылке: без входа и прав на каталог. */
  shared(token: string): Promise<SharedProcess> {
    return this.call("get", `/api/catalog/shared/${encodeURIComponent(token)}`, undefined, SharedProcessSchema);
  }

  sharedObject(token: string, nodeId: string): Promise<ObjectCard> {
    const path = `/api/catalog/shared/${encodeURIComponent(token)}/nodes/${nodeId}/object`;
    return this.call("get", path, undefined, ObjectCardSchema);
  }

  // --- подключения: общий API брокера под префиксом каталога ---

  sourceKinds(): Promise<string[]> {
    return this.call("get", "/api/catalog/source-kinds", undefined, z.array(z.string()));
  }

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

  // --- снимки подключений и синхронизация ---

  /** Подключения с версиями снимка: имя и вид на момент последнего снятия. */
  synced(): Promise<SyncedConnection[]> {
    return this.call("get", "/api/catalog/synced", undefined, z.array(SyncedConnectionSchema));
  }

  connectionVersions(connectionId: string): Promise<ConnectionVersion[]> {
    const path = `/api/catalog/connections/${connectionId}/versions`;
    return this.call("get", path, undefined, z.array(ConnectionVersionSchema));
  }

  /** Все версии снимка подключения; 409 — оно стоит в узлах процессов. */
  forgetVersions(connectionId: string): Promise<number> {
    const path = `/api/catalog/connections/${connectionId}/versions`;
    return this.call("delete", path, undefined, ForgottenSchema).then((result) => result.versions);
  }

  /** Дети узла дерева версии; version < 0 — последняя. */
  connectionTree(connectionId: string, version: number, path: string[]): Promise<TreeNode[]> {
    const query = treeQuery(path, { version: String(version) });
    const url = `/api/catalog/connections/${connectionId}/tree?${query}`;
    return this.call("get", url, undefined, z.array(TreeNodeSchema));
  }

  connectionObject(connectionId: string, version: number, kind: ObjectKind, path: string[]): Promise<ObjectCard> {
    const query = treeQuery(path, { version: String(version), kind });
    return this.call("get", `/api/catalog/connections/${connectionId}/object?${query}`, undefined, ObjectCardSchema);
  }

  connectionDiff(connectionId: string, oldVersion: number, newVersion: number): Promise<SourceDiff> {
    const query = new URLSearchParams({ old: String(oldVersion), new: String(newVersion) });
    const url = `/api/catalog/connections/${connectionId}/diff?${query.toString()}`;
    return this.call("get", url, undefined, SourceDiffSchema);
  }

  connectionSyncs(connectionId: string): Promise<Sync[]> {
    return this.call("get", `/api/catalog/connections/${connectionId}/syncs`, undefined, z.array(SyncSchema));
  }

  startSync(connectionId: string, scope: SyncScope): Promise<Sync> {
    return this.call("post", `/api/catalog/connections/${connectionId}/syncs`, scope, SyncSchema);
  }

  sync(syncId: string): Promise<Sync> {
    return this.call("get", `/api/catalog/syncs/${syncId}`, undefined, SyncSchema);
  }

  cancelSync(syncId: string): Promise<Sync> {
    return this.call("delete", `/api/catalog/syncs/${syncId}`, undefined, SyncSchema);
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
