import { z } from "zod";

import type { PageUrls } from "../config";
import type { paths } from "./schema";

/** Ошибка валидации одного поля: путь по телу запроса (без «body») и причина. */
export type FieldIssue = {
  loc: (string | number)[];
  message: string;
};

const ValidationErrorSchema = z.array(z.object({ loc: z.array(z.union([z.string(), z.number()])), msg: z.string() }));

/** Отказ API: статус, текст detail, тело ответа целиком (409/422 несут
 * данные) и разобранные ошибки полей. */
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
    const detail = ApiError.detailOf(payload);
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

  /** Текст любой ошибки для человека: detail отказа api, message исключения. */
  static describe(error: unknown): string {
    if (error instanceof ApiError) {
      return error.detail;
    }

    if (error instanceof Error) {
      return error.message;
    }

    return String(error);
  }

  /** detail ответа как есть: строка, список ошибок валидации или что-то ещё. */
  private static detailOf(payload: unknown): unknown {
    if (typeof payload === "object" && payload !== null && "detail" in payload) {
      return payload.detail;
    }

    return payload;
  }
}

/** Метка своего запроса — как OwnRequest на сервере: без неё вход, выход и
 * повторный обмен отвергаются, кросс-сайтовая форма её поставить не может. */
export const OWN_REQUEST = { header: "x-boba-request", value: "1" } as const;

export type Method = "get" | "post" | "put" | "delete";

/** Пути схемы, у которых есть операция метода M. */
export type PathWith<M extends Method> = {
  [P in keyof paths]: paths[P] extends Record<M, unknown> ? P : never;
}[keyof paths];

type Operation<P extends keyof paths, M extends Method> = paths[P] extends Record<M, infer O> ? O : never;

type JsonOf<O> = O extends { responses: { 200: { content: { "application/json": infer R } } } } ? R : never;

/** Тело успешного ответа операции по схеме. */
export type Reply<P extends keyof paths, M extends Method> = JsonOf<Operation<P, M>>;

/** Параметры пути из плейсхолдеров `{name}`. */
export type PathParams<P extends string> = P extends `${string}{${infer Name}}${infer Rest}`
  ? Record<Name, string | number> & PathParams<Rest>
  : Record<never, never>;

/** Строка запроса: список повторяет ключ на каждое значение. */
export type Query = Record<string, string | number | readonly string[]>;

/** Путь схемы с подставленными параметрами и строкой запроса. */
export function route<P extends keyof paths>(path: P, params: PathParams<P>, query?: Query): string {
  let built: string = path;
  for (const [name, value] of Object.entries(params as Record<string, string | number>)) {
    built = built.replace(`{${name}}`, encodeURIComponent(String(value)));
  }

  if (query === undefined) {
    return built;
  }

  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    if (typeof value === "string" || typeof value === "number") {
      search.set(name, String(value));
      continue;
    }

    for (const item of value) {
      search.append(name, item);
    }
  }

  const encoded = search.toString();
  if (encoded === "") {
    return built;
  }

  return `${built}?${encoded}`;
}

/** Обмен JSON с api studio: один на страницу, клиенты (WorkflowApi,
 * CatalogApi) — обёртки над ним. Ответ разбирает zod на границе, отказ —
 * ApiError; 401 зовёт обработчик входа, общий для всех клиентов. */
export class HttpTransport {
  private unauthorized: (() => void) | null = null;

  constructor(private readonly urls: PageUrls) {}

  /** Кого звать на 401: страница уводит на вход. */
  onUnauthorized(handler: (() => void) | null): void {
    this.unauthorized = handler;
  }

  /** Сессии больше нет: увести на вход, как на 401. */
  signOut(): void {
    if (this.unauthorized !== null) {
      this.unauthorized();
    }
  }

  async call<T>(method: Method, path: string, body: unknown, schema: z.ZodType<T>): Promise<T> {
    const headers: Record<string, string> = { [OWN_REQUEST.header]: OWN_REQUEST.value };
    const init: RequestInit = { method: method.toUpperCase(), credentials: "same-origin", headers };
    if (body !== undefined) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    const response = await fetch(this.urls.api(path), init);
    if (response.status === 204) {
      return schema.parse(undefined);
    }

    const text = await response.text();
    let payload: unknown = null;
    if (text !== "") {
      payload = JSON.parse(text);
    }

    if (!response.ok) {
      if (response.status === 401 && this.unauthorized !== null) {
        this.unauthorized();
      }

      throw ApiError.of(response.status, payload);
    }

    return schema.parse(payload);
  }
}
