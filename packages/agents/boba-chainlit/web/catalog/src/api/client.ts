import { z } from "zod";

import type { PageUrls } from "../config";
import {
  AccessSchema,
  CatalogChangedSchema,
  DraftSchema,
  DraftStateSchema,
  RebaseResultSchema,
  ShareSchema,
  SnapshotSchema,
  VersionSchema,
  ViewLayoutSchema,
  ViewSchema,
  ViewStateSchema,
  type Access,
  type CatalogChanged,
  type Draft,
  type DraftState,
  type NodePosition,
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
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly payload: unknown,
  ) {
    super(`${status}: ${detail}`);
  }

  static of(status: number, payload: unknown): ApiError {
    const detail = detailOf(payload);
    if (typeof detail === "string") {
      return new ApiError(status, detail, payload);
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
