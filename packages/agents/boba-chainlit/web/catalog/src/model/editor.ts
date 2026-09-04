import { ApiError, type CatalogApi } from "../api/client";
import type { DraftState } from "./catalog";
import type { CatalogOp } from "./ops";

/** Как порция закончилась: принята, отвергнута сервером по существу, черновик закрыт. */
export type ApplyOutcome = { kind: "applied"; state: DraftState } | { kind: "rejected"; reason: string };

/** Правки черновика со страницы: порции идут с expected_seq по одной, а отказ
 * 409 (кто-то дописал раньше) перечитывает черновик и повторяет ту же порцию
 * поверх чужих правок. Ничего локального при этом не теряется: порция либо
 * ляжет следующей, либо будет отвергнута по существу (422) с причиной. */
export class DraftEditor {
  static readonly ATTEMPTS = 3;

  private chain: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly api: CatalogApi,
    private readonly draftId: string,
    private state: DraftState,
    private readonly onState: (state: DraftState) => void,
  ) {}

  get current(): DraftState {
    return this.state;
  }

  /** Перечитать черновик: после чужой правки по событию или для сброса. */
  refresh(): Promise<DraftState> {
    return this.enqueue(async () => {
      const state = await this.api.draft(this.draftId);
      this.take(state);
      return state;
    });
  }

  /** Порция операций поверх текущего seq; порции идут строго по очереди. */
  apply(ops: CatalogOp[]): Promise<ApplyOutcome> {
    return this.enqueue(() => this.send(ops));
  }

  private async send(ops: CatalogOp[]): Promise<ApplyOutcome> {
    for (let attempt = 1; attempt <= DraftEditor.ATTEMPTS; attempt += 1) {
      try {
        const state = await this.api.appendOps(this.draftId, this.state.seq, ops);
        this.take(state);
        return { kind: "applied", state };
      } catch (error: unknown) {
        if (!(error instanceof ApiError)) {
          throw error;
        }

        if (error.status === 409 && attempt < DraftEditor.ATTEMPTS) {
          this.take(await this.api.draft(this.draftId));
          continue;
        }

        if (error.status === 422 || error.status === 409) {
          return { kind: "rejected", reason: error.detail };
        }

        throw error;
      }
    }

    return { kind: "rejected", reason: "the draft keeps changing under us; try again" };
  }

  private take(state: DraftState): void {
    this.state = state;
    this.onState(state);
  }

  private enqueue<T>(work: () => Promise<T>): Promise<T> {
    const next = this.chain.then(work, work);
    this.chain = next.catch(() => undefined);
    return next;
  }
}
