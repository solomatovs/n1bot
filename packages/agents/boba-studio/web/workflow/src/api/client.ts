import { z } from "zod";

import type { PageUrls } from "../config";
import {
  DeletedSchema,
  RunStartedSchema,
  RunStateSchema,
  StoppedSchema,
  StoredRunSchema,
  StoredWorkflowSchema,
  ToolCatalogSchema,
  looseViews,
  withKnownResults,
  type RunState,
  type StoredRun,
  type StoredWorkflow,
  type ToolCatalog,
} from "../model/workflow";
import type { paths } from "./schema";

/** Отказ API: статус и текст detail сервера. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
  }
}

type Method = "get" | "post" | "delete";

/** Пути схемы, у которых есть операция метода M. */
type PathWith<M extends Method> = {
  [P in keyof paths]: paths[P] extends Record<M, unknown> ? P : never;
}[keyof paths];

type Operation<P extends keyof paths, M extends Method> = paths[P] extends Record<M, infer O> ? O : never;

type JsonOf<O> = O extends { responses: { 200: { content: { "application/json": infer R } } } } ? R : never;

/** Тело успешного ответа операции по схеме. */
export type Reply<P extends keyof paths, M extends Method> = JsonOf<Operation<P, M>>;

/** Параметры пути из плейсхолдеров `{name}`. */
type PathParams<P extends string> = P extends `${string}{${infer Name}}${infer Rest}`
  ? Record<Name, string | number> & PathParams<Rest>
  : Record<never, never>;

type Query = Record<string, string | number>;

/** Путь схемы с подставленными параметрами. */
function route<P extends keyof paths>(path: P, params: PathParams<P>): string {
  let built: string = path;
  for (const [name, value] of Object.entries(params as Record<string, string | number>)) {
    built = built.replace(`{${name}}`, encodeURIComponent(String(value)));
  }

  return built;
}

/** REST workflow: пути и типы ответов — из OpenAPI, разбор — zod на границе. */
export class WorkflowApi {
  constructor(private readonly urls: PageUrls) {}

  async catalog(): Promise<ToolCatalog> {
    const raw = await this.raw("get", "/v1/tools", {}, undefined, undefined);
    return ToolCatalogSchema.parse(looseViews(raw));
  }

  async validate(spec: string): Promise<RunState> {
    const raw = await this.raw("post", "/v1/workflows/validate", {}, undefined, { spec });
    return RunStateSchema.parse(withKnownResults(raw));
  }

  listWorkflows(): Promise<StoredWorkflow[]> {
    return this.call("get", "/v1/workflows", {}, undefined, undefined, StoredWorkflowSchema.array());
  }

  getWorkflow(id: number): Promise<StoredWorkflow> {
    return this.call("get", "/v1/workflows/{workflow_id}", { workflow_id: id }, undefined, undefined, StoredWorkflowSchema);
  }

  save(spec: string, layout: Record<string, unknown>): Promise<StoredWorkflow> {
    return this.call("post", "/v1/workflows", {}, undefined, { spec, layout }, StoredWorkflowSchema);
  }

  async remove(id: number): Promise<boolean> {
    const reply = await this.call("delete", "/v1/workflows/{workflow_id}", { workflow_id: id }, undefined, undefined, DeletedSchema);
    return reply.deleted;
  }

  async run(id: number): Promise<string> {
    const reply = await this.call("post", "/v1/workflows/{workflow_id}/run", { workflow_id: id }, undefined, {}, RunStartedSchema);
    return reply.run_id;
  }

  async listRuns(limit = 50): Promise<StoredRun[]> {
    const raw = await this.raw("get", "/v1/workflow-runs", {}, { limit }, undefined);
    return StoredRunSchema.array().parse(z.array(z.unknown()).parse(raw).map(withKnownResults));
  }

  async getRun(runId: string): Promise<StoredRun> {
    const raw = await this.raw("get", "/v1/workflow-runs/{run_id}", { run_id: runId }, undefined, undefined);
    return StoredRunSchema.parse(withKnownResults(raw));
  }

  async stop(runId: string): Promise<boolean> {
    const reply = await this.call("post", "/v1/workflow-runs/{run_id}/stop", { run_id: runId }, undefined, {}, StoppedSchema);
    return reply.stopped;
  }

  /** Ответ, чья zod-модель обязана укладываться в тип ответа по схеме. */
  private async call<M extends Method, P extends PathWith<M>, T extends Reply<P, M>>(
    method: M,
    path: P,
    params: PathParams<P>,
    query: Query | undefined,
    body: unknown,
    schema: z.ZodType<T>,
  ): Promise<T> {
    return schema.parse(await this.raw(method, path, params, query, body));
  }

  /** Сырой JSON ответа: для моделей, которые страница дочитывает сама (итоги инструментов). */
  private async raw<M extends Method, P extends PathWith<M>>(
    method: M,
    path: P,
    params: PathParams<P>,
    query: Query | undefined,
    body: unknown,
  ): Promise<unknown> {
    const init: RequestInit = { method: method.toUpperCase(), credentials: "same-origin" };
    if (body !== undefined) {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(body);
    }

    let url = this.urls.api(route(path, params));
    if (query !== undefined) {
      const search = new URLSearchParams();
      for (const [name, value] of Object.entries(query)) {
        search.set(name, String(value));
      }
      url = `${url}?${search.toString()}`;
    }

    const response = await fetch(url, init);
    const payload: unknown = await response.json();
    if (!response.ok) {
      throw new ApiError(response.status, detailOf(payload));
    }

    return payload;
  }
}

function detailOf(payload: unknown): string {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const detail: unknown = payload.detail;
    if (typeof detail === "string") {
      return detail;
    }

    return JSON.stringify(detail);
  }

  return "request failed";
}
