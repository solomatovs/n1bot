import { z } from "zod";

/** Что сервер вписывает в index.html: префикс приложения и путь socket.io. */
const PageConfigSchema = z.object({
  prefix: z.string(),
  socketPath: z.string(),
});

export type PageConfig = z.infer<typeof PageConfigSchema>;

declare global {
  // eslint-disable-next-line @typescript-eslint/consistent-type-definitions -- дополнение глобального типа
  interface Window {
    __BOBA_PAGE__?: unknown;
  }
}

const DEV_FALLBACK: PageConfig = { prefix: "", socketPath: "/ws/socket.io" };

export function pageConfig(): PageConfig {
  const stamped = window.__BOBA_PAGE__;
  if (stamped === undefined) {
    return DEV_FALLBACK;
  }

  return PageConfigSchema.parse(stamped);
}

/** Адреса страницы и API относительно префикса приложения. */
export class PageUrls {
  constructor(private readonly config: PageConfig) {}

  get routerBase(): string {
    return `${this.config.prefix}/workflow`;
  }

  api(path: string): string {
    return `${this.config.prefix}${path}`;
  }

  get socketPath(): string {
    return this.config.socketPath;
  }

  chat(threadId: string): string {
    return `${this.config.prefix}/thread/${threadId}`;
  }
}
