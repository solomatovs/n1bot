import { describe, expect, it } from "vitest";

import type { EditableTask, EditableWorkflow } from "../../model/spec";
import { streamChains } from "./flow";

function task(name: string): EditableTask {
  return { name, tool: "bash", args: {}, ports: {} };
}

function fd(taskName: string, name: string) {
  return { task: taskName, kind: "fd" as const, name };
}

function ctrl(taskName: string) {
  return { task: taskName, kind: "task" as const, name: "" };
}

function wf(tasks: string[], edges: EditableWorkflow["edges"]): EditableWorkflow {
  return { name: "w", description: "", tasks: tasks.map(task), edges };
}

describe("streamChains", () => {
  it("groups stream-linked tasks in flow order", () => {
    const chains = streamChains(wf(["b", "a"], [{ src: fd("a", "out"), dst: fd("b", "src") }]));
    expect(chains).toEqual([["a", "b"]]);
  });

  it("ignores control edges and lone tasks", () => {
    const chains = streamChains(wf(["a", "b", "c"], [{ src: ctrl("a"), dst: ctrl("b") }]));
    expect(chains).toEqual([]);
  });

  it("keeps separate chains apart", () => {
    const chains = streamChains(
      wf(
        ["a", "b", "c", "d"],
        [
          { src: fd("a", "o"), dst: fd("b", "i") },
          { src: fd("c", "o"), dst: fd("d", "i") },
        ],
      ),
    );
    expect(chains).toEqual([
      ["a", "b"],
      ["c", "d"],
    ]);
  });

  it("chains three tasks end to end", () => {
    const chains = streamChains(
      wf(
        ["mid", "head", "tail"],
        [
          { src: fd("mid", "o"), dst: fd("tail", "i") },
          { src: fd("head", "o"), dst: fd("mid", "i") },
        ],
      ),
    );
    expect(chains).toEqual([["head", "mid", "tail"]]);
  });
});
