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

/** Отказ API: статус и текст detail сервера. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
  }
}

type Method = "GET" | "POST" | "DELETE";

/** REST workflow: один разбор ответа на границе, модели дальше типизированы. */
export class WorkflowApi {
  constructor(private readonly urls: PageUrls) {}

  async catalog(): Promise<ToolCatalog> {
    const raw = await this.call("GET", "/workflows/catalog", undefined, z.unknown());
    return ToolCatalogSchema.parse(looseViews(raw));
  }

  validate(spec: string): Promise<RunState> {
    return this.call("POST", "/workflows/validate", { spec }, RunStateSchema);
  }

  listWorkflows(): Promise<StoredWorkflow[]> {
    return this.call("GET", "/workflows", undefined, StoredWorkflowSchema.array());
  }

  getWorkflow(id: number): Promise<StoredWorkflow> {
    return this.call("GET", `/workflows/${id}`, undefined, StoredWorkflowSchema);
  }

  save(spec: string, layout: Record<string, unknown>): Promise<StoredWorkflow> {
    return this.call("POST", "/workflows", { spec, layout }, StoredWorkflowSchema);
  }

  async remove(id: number): Promise<boolean> {
    const reply = await this.call("DELETE", `/workflows/${id}`, undefined, DeletedSchema);
    return reply.deleted;
  }

  async run(id: number): Promise<string> {
    const reply = await this.call("POST", `/workflows/${id}/run`, {}, RunStartedSchema);
    return reply.run_id;
  }

  async listRuns(limit = 50): Promise<StoredRun[]> {
    const raw = await this.call("GET", `/workflow-runs?limit=${limit}`, undefined, z.array(z.unknown()));
    return StoredRunSchema.array().parse(raw.map(withKnownResults));
  }

  async getRun(runId: string): Promise<StoredRun> {
    const raw = await this.call("GET", `/workflow-runs/${runId}`, undefined, z.unknown());
    return StoredRunSchema.parse(withKnownResults(raw));
  }

  async stop(runId: string): Promise<boolean> {
    const reply = await this.call("POST", `/workflow-runs/${runId}/stop`, {}, StoppedSchema);
    return reply.stopped;
  }

  private async call<T>(
    method: Method,
    path: string,
    body: unknown,
    schema: z.ZodType<T>,
  ): Promise<T> {
    const init: RequestInit = { method, credentials: "same-origin" };
    if (body !== undefined) {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(body);
    }

    const response = await fetch(this.urls.api(path), init);
    const payload: unknown = await response.json();
    if (!response.ok) {
      throw new ApiError(response.status, detailOf(payload));
    }

    return schema.parse(payload);
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
