import { describe, expect, it } from "vitest";

import { resultFlowLabel, resultSummary } from "./results";
import { TaskStateSchema, type TaskState } from "./workflow";

const base = { ok: true, elapsed_ms: 3, metadata: {} };
const idle = { status: "done" as const, call_id: "", started_at: null, finished_at: null, error: "" };

describe("resultSummary", () => {
  it("takes the summary the result rendered for the page", () => {
    const table: TaskState = {
      ...idle,
      result: { kind: "table", ...base, rows: [{ a: 1 }, { a: 2 }], note: null },
      view: { summary: { figure: "2", detail: "rows" }, blocks: [{ block: "grid", rows: [{ a: 1 }, { a: 2 }] }] },
    };
    expect(resultSummary(table)).toEqual({ kind: "table", figure: "2", detail: "rows" });
    expect(resultFlowLabel(table)).toBe("table ×2");

    const visual: TaskState = {
      ...idle,
      result: { kind: "visual", ...base, element: "plotly", props: {}, title: "" },
      view: { summary: { figure: "plotly", detail: "" }, blocks: [{ block: "widget", element: "plotly", props: {}, title: "" }] },
    };
    expect(resultFlowLabel(visual)).toBe("visual");
    expect(resultSummary({ ...idle, result: null, view: null })).toBeNull();
  });

  it("parses a result of a kind the page does not know", () => {
    const raw = {
      ...idle,
      result: { kind: "hologram", ...base, x: 1 },
      view: { summary: { figure: "1", detail: "thing" }, blocks: [{ block: "note", text: "hologram" }] },
    };
    const state = TaskStateSchema.parse(raw);
    expect(state.result?.kind).toBe("hologram");
    expect(state.view?.blocks[0]?.block).toBe("note");
  });
});
