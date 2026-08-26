import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { ReactElement } from "react";

import { jsonRows } from "../../model/json";
import { JsonView } from "../JsonView";
import { HandleSide, handleId } from "./handles";

export type EditorTaskData = {
  name: string;
  tool: string;
  /** Аргументы, в которые можно завести значение: из каталога плюс заданные в задаче. */
  argNames: string[];
  /** Заданные в задаче аргументы: показываются в узле структурно. */
  args: Record<string, unknown>;
  readPorts: string[];
  writePorts: string[];
  selected: boolean;
  issue: string;
};

export type EditorTaskFlowNode = Node<EditorTaskData, "editorTask">;

const HANDLE_ROW = 18;
const HEADER = 40;
const ARG_ROW = 16;
const ARGS_FRAME = 9;
const ARG_CLIP = 40;

export type EditorPort = {
  id: string;
  label: string;
  /** Центр handle по вертикали от верха узла. */
  top: number;
};

type PortLists = Pick<EditorTaskData, "argNames" | "readPorts" | "writePorts">;

/** Порты узла в порядке рядов: слева входы (задача, аргументы, читающие потоки), справа выходы. */
export function editorPorts(data: PortLists): { inputs: EditorPort[]; outputs: EditorPort[] } {
  const inputs = [
    { id: handleId(HandleSide.in, { kind: "task", name: "" }), label: "after" },
    ...data.argNames.map((name) => ({ id: handleId(HandleSide.in, { kind: "arg", name }), label: name })),
    ...data.readPorts.map((name) => ({ id: handleId(HandleSide.in, { kind: "fd", name }), label: `${name} ◂` })),
  ];
  const outputs = [
    { id: handleId(HandleSide.out, { kind: "task", name: "" }), label: "then" },
    { id: handleId(HandleSide.out, { kind: "result", name: "" }), label: "result" },
    ...data.writePorts.map((name) => ({ id: handleId(HandleSide.out, { kind: "fd", name }), label: `▸ ${name}` })),
  ];

  return {
    inputs: inputs.map((port, index) => ({ ...port, top: portTop(index) })),
    outputs: outputs.map((port, index) => ({ ...port, top: portTop(index) })),
  };
}

function portTop(index: number): number {
  return HEADER + HANDLE_ROW * index + HANDLE_ROW / 2;
}

/** Узел редактора: слева входы (задача, аргументы, читающие порты), справа выходы. */
export function EditorTaskNode({ data }: NodeProps<EditorTaskFlowNode>): ReactElement {
  const { inputs, outputs } = editorPorts(data);

  return (
    <div
      className="editor-node"
      data-selected={data.selected}
      data-issue={data.issue !== ""}
      title={data.issue}
    >
      <div className="editor-node__header">
        <div className="editor-node__eyebrow">{data.tool}</div>
        <div className="editor-node__name">{data.name}</div>
      </div>
      <div className="editor-node__ports">
        <div className="editor-node__column">
          {inputs.map((input) => (
            <div className="editor-node__port" key={input.id}>
              <Handle type="target" id={input.id} position={Position.Left} style={{ top: input.top }} />
              <span>{input.label}</span>
            </div>
          ))}
        </div>
        <div className="editor-node__column editor-node__column--right">
          {outputs.map((output) => (
            <div className="editor-node__port editor-node__port--right" key={output.id}>
              <span>{output.label}</span>
              <Handle type="source" id={output.id} position={Position.Right} style={{ top: output.top }} />
            </div>
          ))}
        </div>
      </div>
      {argRows(data.args) > 0 && (
        <div className="editor-node__args">
          <JsonView value={data.args} clip={ARG_CLIP} />
        </div>
      )}
    </div>
  );
}

function argRows(args: Record<string, unknown>): number {
  if (Object.keys(args).length === 0) {
    return 0;
  }

  return jsonRows(args, ARG_CLIP).length;
}

export function editorNodeHeight(
  data: Pick<EditorTaskData, "argNames" | "args" | "readPorts" | "writePorts">,
): number {
  const rows = Math.max(1 + data.argNames.length + data.readPorts.length, 2 + data.writePorts.length);
  const ports = HEADER + HANDLE_ROW * rows + 10;
  const args = argRows(data.args);
  if (args === 0) {
    return ports;
  }

  return ports + ARG_ROW * args + ARGS_FRAME;
}
