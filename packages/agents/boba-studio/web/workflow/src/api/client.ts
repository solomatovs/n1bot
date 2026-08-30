import { z } from "zod";

import type { PageUrls } from "../config";
import type { StopOutcome } from "../model/workflow";
import {
  DeletedSchema,
  RunStartedSchema,
  RunStateSchema,
  StoppedSchema,
  StoredRunSchema,
  StoredWorkflowSchema,
  WorkflowDraftSchema,
  ChannelViewSchema,
  StreamSliceSchema,
  ToolCatalogSchema,
  looseViews,
  withKnownResults,
  type RunState,
  type StoredRun,
  type StoredWorkflow,
  type WorkflowDraft,
  type ChannelView,
  type StreamSlice,
  type ToolCatalog,
} from "../model/workflow";
import {
  ConnectionViewSchema,
  MeSchema,
  ProbeResultSchema,
  ProfileViewSchema,
  SignInProvidersSchema,
  type ConnectionBody,
  type ConnectionView,
  type Me,
  type ProbeResult,
  type ProfileView,
  type SignInProviders,
} from "../model/account";
import type { paths } from "./schema";

/** Ошибка валидации одного поля: путь по телу запроса и причина. */
export type FieldIssue = {
  loc: (string | number)[];
  message: string;
};

const ValidationErrorSchema = z.array(z.object({ loc: z.array(z.union([z.string(), z.number()])), msg: z.string() }));

/** Отказ API: статус, текст detail и разобранные ошибки полей (422). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly issues: FieldIssue[],
  ) {
    super(`${status}: ${detail}`);
  }

  static of(status: number, payload: unknown): ApiError {
    const detail = detailOf(payload);
    const parsed = ValidationErrorSchema.safeParse(detail);
    if (parsed.success) {
      const issues = parsed.data.map((item) => ({ loc: item.loc.filter((part) => part !== "body"), message: item.msg }));
      return new ApiError(status, issues.map((issue) => `${issue.loc.join(".")}: ${issue.message}`).join("\n"), issues);
    }

    if (typeof detail === "string") {
      return new ApiError(status, detail, []);
    }

    return new ApiError(status, JSON.stringify(detail), []);
  }
}

type Method = "get" | "post" | "put" | "delete";

/** Метка своего запроса — как OwnRequest на сервере: без неё вход, выход и повторный
 * обмен отвергаются, кросс-сайтовая форма её поставить не может. */
export const OWN_REQUEST = { header: "x-boba-request", value: "1" } as const;

/** Путь повторного SPNEGO-обмена: роут вне схемы, поэтому не в типах paths. */
const REFRESH_PATH = "/v1/auth/refresh";

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
  private unauthorized: (() => void) | null = null;

  /** profile — выбранный профиль страницы; пусто — сервер берёт профиль по умолчанию. */
  constructor(
    private readonly urls: PageUrls,
    readonly profile = "",
  ) {}

  /** Кого звать на 401: страница уводит на вход. */
  onUnauthorized(handler: (() => void) | null): void {
    this.unauthorized = handler;
  }

  providers(): Promise<SignInProviders> {
    return this.call("get", "/v1/auth/providers", {}, undefined, undefined, SignInProvidersSchema);
  }

  async login(username: string, password: string): Promise<void> {
    await this.raw("post", "/v1/auth/login", {}, undefined, { username, password });
  }

  async logout(): Promise<void> {
    await this.raw("post", "/v1/auth/logout", {}, undefined, {});
  }

  /** Обновление живой сессии по сигналу сервера; true — cookie обновлена.
   * Каким способом — решает сервер по виду входа; на 401 Negotiate браузер отвечает
   * сам. Отказ значит, что сессию не продлить: страница уходит на вход. */
  async refreshSession(): Promise<boolean> {
    const response = await fetch(this.urls.api(REFRESH_PATH), {
      method: "POST",
      credentials: "same-origin",
      headers: { [OWN_REQUEST.header]: OWN_REQUEST.value },
    });
    if (response.status === 204) {
      return true;
    }

    await this.logout().catch(() => undefined);
    if (this.unauthorized !== null) {
      this.unauthorized();
    }

    return false;
  }

  me(): Promise<Me> {
    return this.call("get", "/v1/me", {}, undefined, undefined, MeSchema);
  }

  /** Выбор профиля на пользователе; sid — сокет этой вкладки, чтобы не применять своё же. */
  setProfile(name: string, sid: string): Promise<Me> {
    return this.call("put", "/v1/me/profile", {}, undefined, { profile: name, sid }, MeSchema);
  }

  profiles(): Promise<ProfileView[]> {
    return this.call("get", "/v1/profiles", {}, undefined, undefined, ProfileViewSchema.array());
  }

  /** JSON Schema профиля соединения: по ней строится форма. */
  connectionSchema(): Promise<unknown> {
    return this.raw("get", "/v1/connections/schema", {}, undefined, undefined);
  }

  /** Профиль соединения в схеме — union по kind; страница читает его свободной моделью. */
  async connections(): Promise<ConnectionView[]> {
    const raw = await this.raw("get", "/v1/connections", {}, undefined, undefined);
    return ConnectionViewSchema.array().parse(raw);
  }

  async createConnection(body: ConnectionBody): Promise<ConnectionView> {
    const raw = await this.raw("post", "/v1/connections", {}, undefined, body);
    return ConnectionViewSchema.parse(raw);
  }

  async replaceConnection(id: string, body: ConnectionBody): Promise<ConnectionView> {
    const raw = await this.raw("put", "/v1/connections/{connection_id}", { connection_id: id }, undefined, body);
    return ConnectionViewSchema.parse(raw);
  }

  /** Пробное соединение по черновику формы: исход всегда 200 с ok/message. */
  async checkConnection(profile: Record<string, unknown>): Promise<ProbeResult> {
    const raw = await this.raw("post", "/v1/connections/check", {}, undefined, { profile });
    return ProbeResultSchema.parse(raw);
  }

  checkStoredConnection(id: string): Promise<ProbeResult> {
    return this.call("post", "/v1/connections/{connection_id}/check", { connection_id: id }, undefined, {}, ProbeResultSchema);
  }

  async removeConnection(id: string): Promise<boolean> {
    const reply = await this.call(
      "delete",
      "/v1/connections/{connection_id}",
      { connection_id: id },
      undefined,
      undefined,
      DeletedSchema,
    );
    return reply.deleted;
  }

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

  getWorkflow(id: string): Promise<StoredWorkflow> {
    return this.call("get", "/v1/workflows/{workflow_id}", { workflow_id: id }, undefined, undefined, StoredWorkflowSchema);
  }

  save(spec: string, layout: Record<string, unknown>): Promise<StoredWorkflow> {
    return this.call("post", "/v1/workflows", {}, undefined, { spec, layout }, StoredWorkflowSchema);
  }

  /** Черновик билдера по ключу; нет черновика — null. */
  async getDraft(key: string): Promise<WorkflowDraft | null> {
    try {
      return await this.call("get", "/v1/workflows/drafts/{key}", { key }, undefined, undefined, WorkflowDraftSchema);
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 404) {
        return null;
      }

      throw error;
    }
  }

  /** Запись черновика; sid — сокет этой вкладки, чтобы не применять своё же изменение. */
  putDraft(key: string, spec: string, layout: Record<string, unknown>, sid: string): Promise<WorkflowDraft> {
    return this.call("put", "/v1/workflows/drafts/{key}", { key }, undefined, { spec, layout, sid }, WorkflowDraftSchema);
  }

  async dropDraft(key: string, sid: string): Promise<boolean> {
    const reply = await this.call("delete", "/v1/workflows/drafts/{key}", { key }, { sid }, undefined, DeletedSchema);
    return reply.deleted;
  }

  async remove(id: string): Promise<boolean> {
    const reply = await this.call("delete", "/v1/workflows/{workflow_id}", { workflow_id: id }, undefined, undefined, DeletedSchema);
    return reply.deleted;
  }

  async run(id: string): Promise<string> {
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

  streamChannels(runId: string, callId: string): Promise<ChannelView[]> {
    return this.call(
      "get",
      "/v1/workflow-runs/{run_id}/streams/{call_id}/channels",
      { run_id: runId, call_id: callId },
      undefined,
      undefined,
      ChannelViewSchema.array(),
    );
  }

  /** Окно журнала от смещения: следующий запрос начинается с end прошлого. */
  streamWindow(runId: string, callId: string, channel: string, offset: number): Promise<StreamSlice> {
    return this.call(
      "get",
      "/v1/workflow-runs/{run_id}/streams/{call_id}",
      { run_id: runId, call_id: callId },
      { channel, offset },
      undefined,
      StreamSliceSchema,
    );
  }

  async stop(runId: string): Promise<StopOutcome> {
    const reply = await this.call("post", "/v1/workflow-runs/{run_id}/stop", { run_id: runId }, undefined, {}, StoppedSchema);
    return reply.outcome;
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
    const headers: Record<string, string> = { [OWN_REQUEST.header]: OWN_REQUEST.value };
    const init: RequestInit = { method: method.toUpperCase(), credentials: "same-origin", headers };
    if (body !== undefined) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    let url = this.urls.api(route(path, params));
    const search = new URLSearchParams();
    if (query !== undefined) {
      for (const [name, value] of Object.entries(query)) {
        search.set(name, String(value));
      }
    }
    // профиль метит каждый запрос: сервер читает его из query, если в теле его нет
    if (this.profile !== "") {
      search.set("profile", this.profile);
    }
    const encoded = search.toString();
    if (encoded !== "") {
      url = `${url}?${encoded}`;
    }

    const response = await fetch(url, init);
    if (response.status === 204) {
      return undefined;
    }

    const payload: unknown = await response.json();
    if (!response.ok) {
      if (response.status === 401 && this.unauthorized !== null) {
        this.unauthorized();
      }

      throw ApiError.of(response.status, payload);
    }

    return payload;
  }
}

/** detail ответа как есть: строка, список ошибок валидации или что-то ещё. */
function detailOf(payload: unknown): unknown {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    return payload.detail;
  }

  return "request failed";
}
