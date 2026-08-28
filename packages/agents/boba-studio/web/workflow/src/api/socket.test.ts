import { describe, expect, it } from "vitest";

import { BUS_STATE, busStateOf, lampStatus, streamEventOf } from "./socket";

describe("lampStatus", () => {
  it("shows the link state while the socket is not connected", () => {
    expect(lampStatus("connecting", "connecting", BUS_STATE.listening).state).toBe("connecting");
    expect(lampStatus("disconnected", "gone", BUS_STATE.listening)).toEqual({
      state: "disconnected",
      detail: "gone",
      bus: BUS_STATE.listening,
    });
  });

  it("is connected only when the server listens to the bus", () => {
    expect(lampStatus("connected", "on", BUS_STATE.listening).state).toBe("connected");
    const degraded = lampStatus("connected", "on", BUS_STATE.failed);
    expect(degraded.state).toBe("degraded");
    expect(degraded.detail).toBe("server bus listener is failed");
  });
});

describe("busStateOf", () => {
  it("accepts known listener states and treats the rest as failed", () => {
    expect(busStateOf({ listener: "listening" })).toBe(BUS_STATE.listening);
    expect(busStateOf({ listener: "connecting" })).toBe(BUS_STATE.connecting);
    expect(busStateOf({ listener: "weird" })).toBe(BUS_STATE.failed);
    expect(busStateOf(null)).toBe(BUS_STATE.failed);
  });
});

describe("streamEventOf", () => {
  it("parses a stream event and rejects a malformed one", () => {
    const event = streamEventOf({
      run_id: "run-1",
      call_id: "call-1",
      channel: "tool_stdout",
      size: 12,
      closed: false,
      note: "",
    });
    expect(event).toEqual({
      run_id: "run-1",
      call_id: "call-1",
      channel: "tool_stdout",
      size: 12,
      closed: false,
      note: "",
    });
    expect(streamEventOf({ run_id: "run-1" })).toBeNull();
    expect(streamEventOf(null)).toBeNull();
  });
});
