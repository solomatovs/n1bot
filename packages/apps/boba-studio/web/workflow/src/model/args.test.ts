import { describe, expect, it } from "vitest";

import { blockRows, intentOf, withIntent } from "./args";
import type { EditableTask } from "./spec";
import { FieldEditorSchema, ToolCatalogSchema, looseEditors, type ToolFacts } from "./workflow";

const FACTS: ToolFacts = {
  name: "pg_query",
  availability: "available",
  description: "run sql",
  args: [
    {
      name: "connection_name", required: true, placement: "body", description: "",
      editor: { editor: "connection", family: "postgres" }, display: null,
    },
    {
      name: "sql", required: true, placement: "body", description: "query",
      editor: { editor: "text", multiline: false, placeholder: "" },
      display: { kind: "markdown", ok: true, elapsed_ms: 0, metadata: {}, text: "", language: "sql" },
    },
    {
      name: "intent", required: false, placement: "header", description: "",
      editor: { editor: "text", multiline: false, placeholder: "" }, display: null,
    },
  ],
  ports: [],
  results: ["table", "affected"],
  task_ports: false,
};

const TASK: EditableTask = {
  name: "count",
  tool: "pg_query",
  args: { sql: "select 1", intent: "count rows", extra: 5 },
  ports: {},
};

describe("blockRows", () => {
  it("orders rows by catalog, binds value edges and keeps extras", () => {
    const edges = [{ src: { task: "fetch", kind: "result" as const, name: "" }, dst: { task: "count", kind: "arg" as const, name: "sql" } }];
    const rows = blockRows(TASK, FACTS, edges);

    expect(rows.intent).toBe("count rows");
    expect(rows.body.map((row) => row.name)).toEqual(["connection_name", "sql", "extra"]);
    expect(rows.body[1]?.bound).toBe("fetch.result");
    expect(rows.body[0]?.value).toBeUndefined();
    expect(rows.body[2]?.editor.editor).toBe("text");
  });

  it("intent lives in args and is removed when cleared", () => {
    expect(intentOf(withIntent(TASK, ""))).toBe("");
    expect(withIntent(TASK, "").args).not.toHaveProperty("intent");
    expect(intentOf(withIntent(TASK, "x"))).toBe("x");
  });
});

describe("FieldEditorSchema", () => {
  it("parses known editors and falls back to text for unknown ones", () => {
    expect(FieldEditorSchema.parse({ editor: "number", minimum: 1, maximum: null }).editor).toBe("number");
    const field = { name: "x", required: false, placement: "body", description: "", editor: { editor: "hologram" }, display: null };
    const raw = { t: { ...FACTS, args: [field] } };
    const catalog = ToolCatalogSchema.parse(looseEditors(raw));
    expect(catalog.t?.args[0]?.editor.editor).toBe("text");
  });
});
