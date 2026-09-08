import { z } from "zod";

import type { StopOutcome } from "../model/workflow";
import {
  DeletedSchema,
  RunStartedSchema,
  RunStateSchema,
  StoppedSchema,
  StoredRunSchema,
  StoredWorkflowSchema,
  ChannelViewSchema,
  StreamSliceSchema,
  ToolCatalogSchema,
  looseViews,
  withKnownResults,
  type RunState,
  type StoredRun,
  type StoredWorkflow,
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
import {
  ApiError,
  route,
  type HttpTransport,
  type Method,
  type PathParams,
  type PathWith,
  type Query,
  type Reply,
} from "./transport";

/** Путь повторного SPNEGO-обмена: роут вне схемы, поэтому не в типах paths. */
const REFRESH_PATH = "/v1/auth/refresh";

/** REST workflow и учётной записи: пути и типы ответов — из OpenAPI, разбор —
 * zod на границе; обмен ведёт общий HttpTransport. */
export class WorkflowApi {
  constructor(private readonly transport: HttpTransport) {}

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
    let refused: ApiError;
    try {
      await this.transport.call("post", REFRESH_PATH, undefined, z.unknown());
      return true;
    } catch (error: unknown) {
      if (!(error instanceof ApiError)) {
        throw error;
      }

      refused = error;
    }

    await this.logout().catch(() => undefined);
    // на 401 транспорт уже увёл на вход
    if (refused.status !== 401) {
      this.transport.signOut();
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

  /** Сохранение строки по id: черновик становится истиной той же строки. */
  saveInto(id: string, spec: string, layout: Record<string, unknown>): Promise<StoredWorkflow> {
    return this.call(
      "put",
      "/v1/workflows/{workflow_id}",
      { workflow_id: id },
      undefined,
      { spec, layout },
      StoredWorkflowSchema,
    );
  }

  /** Черновик строки workflow; sid — сокет этой вкладки, чтобы не применять своё же изменение. */
  putWorkflowDraft(id: string, spec: string, layout: Record<string, unknown>, sid: string): Promise<StoredWorkflow> {
    return this.call(
      "put",
      "/v1/workflows/{workflow_id}/draft",
      { workflow_id: id },
      undefined,
      { spec, layout, sid },
      StoredWorkflowSchema,
    );
  }

  /** Сброс черновика: строка возвращается к сохранённому состоянию. */
  clearWorkflowDraft(id: string, sid: string): Promise<StoredWorkflow> {
    return this.call(
      "delete",
      "/v1/workflows/{workflow_id}/draft",
      { workflow_id: id },
      { sid },
      undefined,
      StoredWorkflowSchema,
    );
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
  private call<M extends Method, P extends PathWith<M>, T extends Reply<P, M>>(
    method: M,
    path: P,
    params: PathParams<P>,
    query: Query | undefined,
    body: unknown,
    schema: z.ZodType<T>,
  ): Promise<T> {
    return this.transport.call(method, route(path, params, query), body, schema);
  }

  /** Сырой JSON ответа: для моделей, которые страница дочитывает сама (итоги инструментов). */
  private raw<M extends Method, P extends PathWith<M>>(
    method: M,
    path: P,
    params: PathParams<P>,
    query: Query | undefined,
    body: unknown,
  ): Promise<unknown> {
    return this.transport.call(method, route(path, params, query), body, z.unknown());
  }
}
