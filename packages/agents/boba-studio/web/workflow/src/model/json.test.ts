import { describe, expect, it } from "vitest";

import { clipText, jsonRows } from "./json";

describe("jsonRows", () => {
  it("lays out nested objects with braces and depth", () => {
    const rows = jsonRows({ command: "echo hi", limits: { rows: 10, strict: true }, tags: [] }, 0);
    expect(rows.map((row) => `${row.depth}:${row.key}:${row.kind}:${row.text}`)).toEqual([
      "0::open:{",
      "1:command:string:echo hi",
      "1:limits:open:{",
      "2:rows:number:10",
      "2:strict:boolean:true",
      "1::close:}",
      "1:tags:empty:[]",
      "0::close:}",
    ]);
  });

  it("renders arrays and null", () => {
    const rows = jsonRows([1, null, "x"], 0);
    expect(rows.map((row) => row.kind)).toEqual(["open", "number", "null", "string", "close"]);
  });

  it("clips long and multi-line strings to one line", () => {
    expect(clipText("short", 10)).toBe("short");
    expect(clipText("0123456789abc", 10)).toBe("0123456789…");
    expect(clipText("first\nsecond", 10)).toBe("first…");
    expect(clipText("first\nsecond", 0)).toBe("first\nsecond");
  });
});
