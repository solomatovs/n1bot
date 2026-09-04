import { z } from "zod";

import type { PageUrls } from "../config";
import {
  DraftSchema,
  DraftStateSchema,
  SnapshotSchema,
  VersionSchema,
  ViewLayoutSchema,
  ViewSchema,
  type Draft,
  type DraftState,
  type Snapshot,
  type Version,
  type View,
  type ViewLayout,
} from "../model/catalog";
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
