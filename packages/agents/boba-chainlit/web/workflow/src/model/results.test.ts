import { describe, expect, it } from "vitest";

import { resultFlowLabel, resultSummary } from "./results";
import { TaskStateSchema, withKnownResults, type ToolResult } from "./workflow";

const base = { ok: true, elapsed_ms: 3, metadata: {} };

describe("resultSummary", () => {
  it("counts rows, lines and items by kind", () => {
    const table: ToolResult = { kind: "table", ...base, rows: [{ a: 1 }, { a: 2 }], note: null };
    expect(resultSummary(table)).toEqual({ kind: "table", figure: "2", detail: "rows" });
    const shell: ToolResult = {
      kind: "shell", ...base, exit_code: 0, stdout: "a\nb", stdout_truncated: false, stderr: "",
      stderr_truncated: false, duration_ms: 5, timed_out: false, diagnostic: "",
    };
    expect(resultSummary(shell).figure).toBe("exit 0");
    expect(resultFlowLabel(table)).toBe("table ×2");
    const multi: ToolResult = { kind: "multi", ...base, items: [table, shell] };
    expect(resultSummary(multi)).toEqual({ kind: "multi", figure: "2", detail: "items" });
  });

  it("parses nested multi and unknown kinds from the run state", () => {
    const raw = {
      tasks: {
        a: {
          status: "done", call_id: "", started_at: null, finished_at: null, error: "",
          result: { kind: "multi", ...base, items: [{ kind: "affected", ...base, affected_rows: 3, status: "UPDATE 3" }, { kind: "hologram", ...base, x: 1 }] },
        },
      },
    };
    const normalized = withKnownResults(raw) as { tasks: Record<string, unknown> };
    const state = TaskStateSchema.parse(normalized.tasks.a);
    expect(state.result?.kind).toBe("multi");
    if (state.result?.kind === "multi") {
      expect(state.result.items.map((item) => item.kind)).toEqual(["affected", "opaque"]);
    }
  });
});
