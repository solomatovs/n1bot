import { describe, expect, it } from "vitest";

import { ApiError, type CatalogApi } from "../api/client";
import type { DraftState } from "./catalog";
import { DraftEditor } from "./editor";
import type { CatalogOp } from "./ops";

/** Сервер черновика в памяти: порции с чужим seq отвергаются 409, как настоящий. */
class FakeDraftApi {
  seq = 0;
  portions: CatalogOp[][] = [];
  conflicts = 0;

  state(): DraftState {
    return {
      draft: {
        id: "d",
        name: "d",
        base_version: 0,
        status: "open",
        created_by: "u",
        created_at: "2026-01-01T00:00:00Z",
      },
      snapshot: { layers: {}, datasets: {}, columns: {}, load_kinds: {}, flows: {} },
      diff: { entries: [] },
      seq: this.seq,
    };
  }

  draft(): Promise<DraftState> {
    return Promise.resolve(this.state());
  }

  appendOps(_draftId: string, expectedSeq: number, ops: CatalogOp[]): Promise<DraftState> {
    if (expectedSeq !== this.seq) {
      this.conflicts += 1;
      return Promise.reject(
        new ApiError(409, "draft moved", { detail: { message: "draft moved", current_seq: this.seq } }),
      );
    }

    if (ops.some((op) => op.op === "remove_layer")) {
      return Promise.reject(new ApiError(422, "layer not found", { detail: { message: "layer not found" } }));
    }

    this.seq += 1;
    this.portions.push(ops);
    return Promise.resolve(this.state());
  }

  /** Чужая порция мимо редактора: страница о ней ещё не знает. */
  someoneElse(): void {
    this.seq += 1;
    this.portions.push([{ op: "add_layer", layer: { id: "x", name: "x" } }]);
  }
}

function editorOf(fake: FakeDraftApi): { editor: DraftEditor; seen: number[] } {
  const seen: number[] = [];
  const editor = new DraftEditor(fake as unknown as CatalogApi, "d", fake.state(), (state) => {
    seen.push(state.seq);
  });
  return { editor, seen };
}

describe("DraftEditor", () => {
  it("retries a portion after somebody else appended first", async () => {
    const fake = new FakeDraftApi();
    const { editor, seen } = editorOf(fake);
    fake.someoneElse();

    const outcome = await editor.apply([{ op: "add_layer", layer: { id: "a", name: "a" } }]);

    expect(outcome.kind).toBe("applied");
    expect(fake.conflicts).toBe(1);
    expect(fake.portions).toHaveLength(2);
    expect(fake.portions[1]?.[0]?.op).toBe("add_layer");
    expect(seen.at(-1)).toBe(2);
  });

  it("reports a rejected portion with the server reason and keeps going", async () => {
    const fake = new FakeDraftApi();
    const { editor } = editorOf(fake);

    const rejected = await editor.apply([{ op: "remove_layer", id: "nope" }]);
    const applied = await editor.apply([{ op: "add_layer", layer: { id: "a", name: "a" } }]);

    expect(rejected).toEqual({ kind: "rejected", reason: "layer not found" });
    expect(applied.kind).toBe("applied");
    expect(fake.seq).toBe(1);
  });

  it("sends portions strictly one after another", async () => {
    const fake = new FakeDraftApi();
    const { editor } = editorOf(fake);

    const first = editor.apply([{ op: "add_layer", layer: { id: "a", name: "a" } }]);
    const second = editor.apply([{ op: "add_layer", layer: { id: "b", name: "b" } }]);
    await Promise.all([first, second]);

    expect(fake.conflicts).toBe(0);
    expect(fake.seq).toBe(2);
  });
});
