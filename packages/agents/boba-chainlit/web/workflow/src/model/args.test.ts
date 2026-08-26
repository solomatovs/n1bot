import { describe, expect, it } from "vitest";

import { blockRows, intentOf, withIntent } from "./args";
import type { EditableTask } from "./spec";
import { ArgViewSchema, ToolCatalogSchema, looseViews, type ToolFacts } from "./workflow";

const FACTS: ToolFacts = {
  name: "pg_query",
  availability: "available",
  description: "run sql",
  args: [
    { name: "connection_name", required: true, view: { kind: "connection", placement: "body", family: "postgres" }, description: "" },
    { name: "sql", required: true, view: { kind: "code", placement: "body", lang: "sql" }, description: "query" },
    { name: "intent", required: false, view: { kind: "intent", placement: "header" }, description: "" },
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
    expect(rows.body[2]?.view.kind).toBe("text");
  });

  it("intent lives in args and is removed when cleared", () => {
    expect(intentOf(withIntent(TASK, ""))).toBe("");
    expect(withIntent(TASK, "").args).not.toHaveProperty("intent");
    expect(intentOf(withIntent(TASK, "x"))).toBe("x");
  });
});

describe("ArgViewSchema", () => {
  it("parses every known kind and falls back to text for unknown ones", () => {
    expect(ArgViewSchema.parse({ kind: "number", placement: "body", minimum: 1, maximum: null, unit: "" }).kind).toBe("number");
    const raw = { t: { ...FACTS, args: [{ name: "x", required: false, view: { kind: "hologram" }, description: "" }] } };
    const catalog = ToolCatalogSchema.parse(looseViews(raw));
    expect(catalog.t?.args[0]?.view.kind).toBe("text");
  });
});
