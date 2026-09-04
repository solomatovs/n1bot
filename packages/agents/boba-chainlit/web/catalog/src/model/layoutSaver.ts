import type { CatalogApi } from "../api/client";
import type { NodePosition } from "./catalog";

type Hooks = {
  onSaved: () => void;
  onFailed: (message: string) => void;
};

/** Отложенная запись раскладки вида: серия перетаскиваний схлопывается в один
 * PUT через паузу, очередной запрос ждёт завершения предыдущего. */
export class LayoutSaver {
  static readonly DELAY_MS = 500;

  private timer: number | null = null;
  private pending: { viewId: string; positions: NodePosition[] } | null = null;
  private inflight: Promise<void> = Promise.resolve();

  constructor(
    private readonly api: CatalogApi,
    private readonly hooks: Hooks,
  ) {}

  schedule(viewId: string, positions: NodePosition[]): void {
    this.pending = { viewId, positions };
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
    }

    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.flush();
    }, LayoutSaver.DELAY_MS);
  }

  dispose(): void {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private flush(): void {
    const next = this.pending;
    this.pending = null;
    if (next === null) {
      return;
    }

    this.inflight = this.inflight
      .then(() => this.api.putLayout(next.viewId, next.positions))
      .then(() => {
        this.hooks.onSaved();
      })
      .catch((error: unknown) => {
        this.hooks.onFailed(error instanceof Error ? error.message : String(error));
      });
  }
}
