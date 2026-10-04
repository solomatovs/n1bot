import { describe, expect, it } from "vitest";

import { layoutGraph } from "./layout";
import { WorkflowGraphSchema } from "./workflow";

const GRAPH = WorkflowGraphSchema.parse({
  spec: {
    name: "demo",
    description: "",
    tasks: {
      dump: { tool: "bash", args: { command: "a" }, ports: { out: "write" } },
      load: { tool: "bash", args: { command: "b" }, ports: { src: "read" } },
      ids: { tool: "pg_query", args: { query: "q" }, ports: {} },
      check: { tool: "pg_query", args: { query: "{{ ids }}" }, ports: {} },
    },
    edges: [
      {
        src: { task: "dump", kind: "fd", name: "out" },
        dst: { task: "load", kind: "fd", name: "src" },
        kind: "stream",
      },
      {
        src: { task: "ids", kind: "result", name: "" },
        dst: { task: "check", kind: "arg", name: "query" },
        kind: "value",
      },
      {
        src: { task: "load", kind: "task", name: "" },
        dst: { task: "check", kind: "task", name: "" },
        kind: "control",
      },
    ],
  },
  stages: [
    { id: "stage:dump", tasks: ["dump", "load"], streams: [], after: [] },
    { id: "stage:ids", tasks: ["ids"], streams: [], after: [] },
    { id: "stage:check", tasks: ["check"], streams: [], after: ["stage:dump", "stage:ids"] },
  ],
  bindings: { check: [{ arg: "query", sources: ["ids"], template: "{{ ids }}" }] },
});

describe("layoutGraph", () => {
  const layout = layoutGraph(GRAPH);

  it("places every task inside its stage box", () => {
    const stages = new Map(layout.stages.map((box) => [box.stage, box]));
    for (const task of layout.tasks) {
      const stage = stages.get(task.stage);
      expect(stage).toBeDefined();
      if (stage === undefined) {
        continue;
      }

      expect(task.position.x).toBeGreaterThanOrEqual(0);
      expect(task.position.y).toBeGreaterThanOrEqual(0);
      expect(task.position.x + task.size.width).toBeLessThanOrEqual(stage.size.width);
      expect(task.position.y + task.size.height).toBeLessThanOrEqual(stage.size.height);
    }
  });

  it("orders stages by dependencies left to right", () => {
    const x = new Map(layout.stages.map((box) => [box.stage, box.position.x]));
    expect(x.get("stage:check")).toBeGreaterThan(x.get("stage:dump") ?? Infinity);
    expect(x.get("stage:check")).toBeGreaterThan(x.get("stage:ids") ?? Infinity);
  });

  it("orders streamed tasks inside a stage", () => {
    const byTask = new Map(layout.tasks.map((box) => [box.task, box.position.x]));
    expect(byTask.get("load")).toBeGreaterThan(byTask.get("dump") ?? Infinity);
  });

  it("labels edges by kind", () => {
    const labels = layout.edges.map((edge) => [edge.kind, edge.label]);
    expect(labels).toEqual([
      ["stream", "out → src"],
      ["value", "query"],
      ["control", ""],
    ]);
  });
});
