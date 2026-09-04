import { ApiError, type CatalogApi } from "../api/client";
import type { SourceDraftState, SourceOp } from "./catalog";

export type SourceApplyOutcome = { kind: "applied"; state: SourceDraftState } | { kind: "rejected"; reason: string };

/** Правки черновика ручного источника: та же дисциплина, что у DraftEditor —
 * порции по одной с expected_seq, 409 перечитывает и повторяет, 422 отдаёт
 * причину отказа. */
export class SourceDraftEditor {
  static readonly ATTEMPTS = 3;

  private chain: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly api: CatalogApi,
    private readonly draftId: string,
    private state: SourceDraftState,
    private readonly onState: (state: SourceDraftState) => void,
  ) {}

  get current(): SourceDraftState {
    return this.state;
  }

  refresh(): Promise<SourceDraftState> {
    return this.enqueue(async () => {
      const state = await this.api.sourceDraft(this.draftId);
      this.take(state);
      return state;
    });
  }

  apply(ops: SourceOp[]): Promise<SourceApplyOutcome> {
    return this.enqueue(() => this.send(ops));
  }

  private async send(ops: SourceOp[]): Promise<SourceApplyOutcome> {
    for (let attempt = 1; attempt <= SourceDraftEditor.ATTEMPTS; attempt += 1) {
      try {
        const state = await this.api.appendSourceOps(this.draftId, this.state.seq, ops);
        this.take(state);
        return { kind: "applied", state };
      } catch (error: unknown) {
        if (!(error instanceof ApiError)) {
          throw error;
        }

        if (error.status === 409 && attempt < SourceDraftEditor.ATTEMPTS) {
          this.take(await this.api.sourceDraft(this.draftId));
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

  private take(state: SourceDraftState): void {
    this.state = state;
    this.onState(state);
  }

  private enqueue<T>(work: () => Promise<T>): Promise<T> {
    const next = this.chain.then(work, work);
    this.chain = next.catch(() => undefined);
    return next;
  }
}
