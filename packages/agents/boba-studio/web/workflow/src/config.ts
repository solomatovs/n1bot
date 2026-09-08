import { z } from "zod";

import { ConnectionUrlKey, DIFF_MODE, UrlKey, type PaneTab } from "./catalog/model/urlKeys";

/** Что сервер вписывает в index.html: префикс приложения и путь socket.io. */
const PageConfigSchema = z.object({
  prefix: z.string(),
  apiPrefix: z.string(),
  socketPath: z.string(),
});

export type PageConfig = z.infer<typeof PageConfigSchema>;

declare global {
  // eslint-disable-next-line @typescript-eslint/consistent-type-definitions -- дополнение глобального типа
  interface Window {
    __BOBA_PAGE__?: unknown;
  }
}

const DEV_FALLBACK: PageConfig = {
  prefix: "",
  apiPrefix: "/api",
  socketPath: "/api/socket.io",
};

export function pageConfig(): PageConfig {
  const stamped = window.__BOBA_PAGE__;
  if (stamped === undefined) {
    return DEV_FALLBACK;
  }

  return PageConfigSchema.parse(stamped);
}

type DraftQuery = { pane?: PaneTab };
type ConnectionQuery = { v?: number; mode?: typeof DIFF_MODE };

/** Маршруты каталога относительно базы роутера: ссылки страниц и паттерны
 * Route (в паттерн подставляется `:param`). Строка запроса — часть адреса,
 * которую страница читает через urlState. */
const CatalogUrls = {
  home(): string {
    return "/catalog";
  },

  process(processId: string): string {
    return `/catalog/processes/${processId}`;
  },

  draft(draftId: string, query: DraftQuery = {}): string {
    const search = new URLSearchParams();
    if (query.pane !== undefined) {
      search.set(UrlKey.pane, query.pane);
    }

    return CatalogUrls.withSearch(`/catalog/drafts/${draftId}`, search);
  },

  connections(): string {
    return "/catalog/connections";
  },

  connection(connectionId: string, query: ConnectionQuery = {}): string {
    const search = new URLSearchParams();
    if (query.v !== undefined) {
      search.set(ConnectionUrlKey.version, String(query.v));
    }
    if (query.mode !== undefined) {
      search.set(ConnectionUrlKey.mode, query.mode);
    }

    return CatalogUrls.withSearch(`/catalog/connections/${connectionId}`, search);
  },

  shared(token: string): string {
    return `/catalog/shared/${token}`;
  },

  withSearch(pathname: string, search: URLSearchParams): string {
    const encoded = search.toString();
    if (encoded === "") {
      return pathname;
    }

    return `${pathname}?${encoded}`;
  },
};

/** Адреса страницы и API относительно префикса приложения; пути API — как
 * в OpenAPI (с /v1). Маршруты роутера — статические: они не зависят от
 * префикса, его добавляет BrowserRouter; absolute() даёт адрес наружу. */
export class PageUrls {
  static readonly catalog = CatalogUrls;

  constructor(private readonly config: PageConfig) {}

  static login(): string {
    return "/login";
  }

  static account(): string {
    return "/account";
  }

  /** Билдер: без id — новый workflow. */
  static workflow(workflowId?: string): string {
    if (workflowId === undefined) {
      return "/workflow";
    }

    return `/workflow/${workflowId}`;
  }

  static run(runId: string): string {
    return `/runs/${runId}`;
  }

  get routerBase(): string {
    return `${this.config.prefix}/workflow`;
  }

  api(path: string): string {
    return `${this.config.apiPrefix}${path}`;
  }

  get socketPath(): string {
    return this.config.socketPath;
  }

  /** Полный адрес маршрута страницы с префиксом приложения: ссылки наружу. */
  absolute(route: string): string {
    return `${this.routerBase}${route}`;
  }

  chat(threadId: string): string {
    return `${this.config.prefix}/thread/${threadId}`;
  }
}
