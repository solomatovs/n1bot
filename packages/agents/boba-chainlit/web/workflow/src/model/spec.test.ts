import { describe, expect, it } from "vitest";

import {
  edgeKindOf,
  freeTaskName,
  issueTasks,
  parseEdgeText,
  parseIssues,
  parsePortRef,
  parseSpecText,
  renameTask,
  renderEdge,
  renderSpecText,
} from "./spec";

describe("port references", () => {
  it("parses every port shape and renders it back", () => {
    for (const raw of ["a", "a.result", "a.args.query", "a.out"]) {
      expect(renderEdge({ src: parsePortRef(raw), dst: parsePortRef("b") })).toBe(`${raw} -> b`);
    }

    expect(parsePortRef("a.args.query")).toEqual({ task: "a", kind: "arg", name: "query" });
    expect(parsePortRef("a.out")).toEqual({ task: "a", kind: "fd", name: "out" });
  });

  it("rejects nonsense", () => {
    expect(() => parsePortRef("a.args.x.y")).toThrow();
    expect(() => parsePortRef("1a")).toThrow();
  });

  it("derives edge kinds", () => {
    expect(edgeKindOf("result", "arg")).toBe("value");
    expect(edgeKindOf("fd", "fd")).toBe("stream");
    expect(edgeKindOf("task", "task")).toBe("control");
    expect(edgeKindOf("result", "task")).toBeNull();
  });
});

describe("edge text", () => {
  it("expands lists on either side", () => {
    expect(parseEdgeText("[a, b] -> c").map(renderEdge)).toEqual(["a -> c", "b -> c"]);
    expect(parseEdgeText("a -> [b, c]").map(renderEdge)).toEqual(["a -> b", "a -> c"]);
  });

  it("requires exactly one arrow", () => {
    expect(() => parseEdgeText("a -> b -> c")).toThrow();
    expect(() => parseEdgeText("a b")).toThrow();
  });
});

describe("spec text", () => {
  const TEXT = `name: demo
description: two steps
tasks:
  first:
    tool: bash
    args:
      command: echo one
  second:
    tool: pg_query
    args:
      query: "{{ first }}"
    ports:
      src: read
edges:
  - first.result -> second.args.query
`;

  it("round-trips through the editor model", () => {
    const workflow = parseSpecText(TEXT);
    expect(workflow.tasks.map((task) => task.name)).toEqual(["first", "second"]);
    expect(workflow.edges).toHaveLength(1);
    expect(parseSpecText(renderSpecText(workflow))).toEqual(workflow);
  });

  it("renames tasks in edges and templates", () => {
    const renamed = renameTask(parseSpecText(TEXT), "first", "start");
    expect(renderEdge(renamed.edges[0] ?? { src: parsePortRef("x"), dst: parsePortRef("y") })).toBe(
      "start.result -> second.args.query",
    );
    expect(renamed.tasks[1]?.args.query).toBe("{{ start }}");
  });

  it("finds free task names", () => {
    expect(freeTaskName("bash", ["bash", "bash_2"])).toBe("bash_3");
    expect(freeTaskName("pg_query", [])).toBe("pg_query");
  });
});

describe("issues", () => {
  it("parses server detail and maps issues to tasks", () => {
    const issues = parseIssues(
      "unknown_arg at first: unknown argument: intent; edge_kind at first.result -> second: no such edge; yaml: broken",
    );
    expect(issues).toHaveLength(3);
    expect(issues[0]).toEqual({ code: "unknown_arg", where: "first", message: "unknown argument: intent" });
    expect(issues[2]).toEqual({ code: "yaml", where: "", message: "broken" });

    const workflow = parseSpecText("name: x\ntasks:\n  first: {tool: bash}\n  second: {tool: bash}\n");
    expect([...issueTasks(issues[0] ?? { code: "", where: "", message: "" }, workflow)]).toEqual(["first"]);
    expect([...issueTasks(issues[1] ?? { code: "", where: "", message: "" }, workflow)].sort()).toEqual([
      "first",
      "second",
    ]);
  });
});
