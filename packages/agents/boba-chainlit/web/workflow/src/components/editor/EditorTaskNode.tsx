import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { ReactElement } from "react";

import type { ArgRow } from "../../model/args";
import { widgetOf } from "../args/widgets";
import { HandleSide, handleId } from "./handles";

export type EditorTaskData = {
  name: string;
  tool: string;
  intent: string;
  /** Строки тела в порядке каталога: каждая — порт для ребра-значения. */
  rows: ArgRow[];
  readPorts: string[];
  writePorts: string[];
  results: string[];
  selected: boolean;
  issue: string;
};

export type EditorTaskFlowNode = Node<EditorTaskData, "editorTask">;

const HEADER = 44;
const HEADER_WITH_INTENT = 58;
const ROW = 22;
const FOOTER = 26;
const BODY_PAD = 4;

export type EditorPort = {
  id: string;
  label: string;
  /** Центр handle по вертикали от верха узла. */
  top: number;
};

export type ArgPort = EditorPort & { row: ArgRow };

export type EditorPorts = {
  taskIn: EditorPort;
  taskOut: EditorPort;
  args: ArgPort[];
  reads: EditorPort[];
  writes: EditorPort[];
  result: EditorPort;
};

type Shape = Pick<EditorTaskData, "intent" | "rows" | "readPorts" | "writePorts">;

function headerHeight(data: Shape): number {
  return data.intent === "" ? HEADER : HEADER_WITH_INTENT;
}

/** Геометрия портов: управление на шапке, аргументы и fd-порты строками тела, result в футере. */
export function editorPorts(data: Shape): EditorPorts {
  const header = headerHeight(data);
  const middle = header / 2;
  let top = header + BODY_PAD + ROW / 2;

  const args: ArgPort[] = [];
  for (const row of data.rows) {
    args.push({ id: handleId(HandleSide.in, { kind: "arg", name: row.name }), label: row.name, top, row });
    top += ROW;
  }

  const reads: EditorPort[] = [];
  for (const name of data.readPorts) {
    reads.push({ id: handleId(HandleSide.in, { kind: "fd", name }), label: `${name} ◂`, top });
    top += ROW;
  }

  const writes: EditorPort[] = [];
  for (const name of data.writePorts) {
    writes.push({ id: handleId(HandleSide.out, { kind: "fd", name }), label: `▸ ${name}`, top });
    top += ROW;
  }

  const footerTop = top - ROW / 2 + BODY_PAD + FOOTER / 2;

  return {
    taskIn: { id: handleId(HandleSide.in, { kind: "task", name: "" }), label: "run after", top: middle },
    taskOut: { id: handleId(HandleSide.out, { kind: "task", name: "" }), label: "then", top: middle },
    args,
    reads,
    writes,
    result: { id: handleId(HandleSide.out, { kind: "result", name: "" }), label: "result", top: footerTop },
  };
}

export function editorNodeHeight(data: Shape): number {
  const rows = data.rows.length + data.readPorts.length + data.writePorts.length;
  return headerHeight(data) + BODY_PAD * 2 + ROW * rows + FOOTER;
}

function RowValue({ row }: { row: ArgRow }): ReactElement {
  if (row.bound !== "") {
    return <span className="arg-row__value arg-row__value--bound">◂ {row.bound}</span>;
  }

  if (row.value === undefined) {
    return <span className="arg-row__value arg-row__value--empty">{row.required ? "required" : "—"}</span>;
  }

  const { Row } = widgetOf(row.view);
  return <Row view={row.view} value={row.value} />;
}

/** Узел редактора: шапка (инструмент, имя, intent) с управлением, строки-порты, футер result. */
export function EditorTaskNode({ data }: NodeProps<EditorTaskFlowNode>): ReactElement {
  const ports = editorPorts(data);

  return (
    <div
      className="editor-node"
      data-selected={data.selected}
      data-issue={data.issue !== ""}
      title={data.issue}
    >
      <div className="editor-node__header" style={{ height: headerHeight(data) }}>
        <Handle type="target" id={ports.taskIn.id} position={Position.Left} style={{ top: ports.taskIn.top }} />
        <div className="editor-node__eyebrow">{data.tool}</div>
        <div className="editor-node__name">{data.name}</div>
        {data.intent !== "" && <div className="editor-node__intent">{data.intent}</div>}
        <Handle type="source" id={ports.taskOut.id} position={Position.Right} style={{ top: ports.taskOut.top }} />
      </div>
      <div className="editor-node__rows">
        {ports.args.map((port) => (
          <div className="arg-row" data-arg={port.row.name} data-required={port.row.required} key={port.id}>
            <Handle type="target" id={port.id} position={Position.Left} style={{ top: port.top }} />
            <span className="arg-row__key">{port.row.name}</span>
            <RowValue row={port.row} />
          </div>
        ))}
        {ports.reads.map((port) => (
          <div className="arg-row arg-row--port" key={port.id}>
            <Handle type="target" id={port.id} position={Position.Left} style={{ top: port.top }} />
            <span className="arg-row__key">{port.label}</span>
          </div>
        ))}
        {ports.writes.map((port) => (
          <div className="arg-row arg-row--port arg-row--right" key={port.id}>
            <span className="arg-row__key">{port.label}</span>
            <Handle type="source" id={port.id} position={Position.Right} style={{ top: port.top }} />
          </div>
        ))}
      </div>
      <div className="editor-node__footer">
        <span className="editor-node__results">
          {data.results.map((kind) => (
            <span className="chip" key={kind}>
              {kind}
            </span>
          ))}
        </span>
        <span className="editor-node__result">result</span>
        <Handle type="source" id={ports.result.id} position={Position.Right} style={{ top: ports.result.top }} />
      </div>
    </div>
  );
}
