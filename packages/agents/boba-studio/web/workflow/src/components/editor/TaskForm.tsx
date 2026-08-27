import { Trash2, X } from "lucide-react";
import { type ReactElement, useState } from "react";

import { blockRows, intentOf, withIntent, type ArgRow } from "../../model/args";
import { isIdent, type EditableEdge, type EditableTask } from "../../model/spec";
import type { PortDirection, ToolCatalog } from "../../model/workflow";
import { widgetOf } from "../args/widgets";

type Props = {
  task: EditableTask;
  catalog: ToolCatalog;
  edges: EditableEdge[];
  taken: string[];
  onChange: (task: EditableTask) => void;
  onRename: (to: string) => void;
  onRemove: () => void;
  onClose: () => void;
};

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([name]) => name !== key));
}

type FieldProps = {
  row: ArgRow;
  known: boolean;
  onValue: (value: unknown) => void;
  onClear: () => void;
};

/** Поле аргумента: виджет по виду; привязанный ребром аргумент показывает источник. */
function ArgField({ row, known, onValue, onClear }: FieldProps): ReactElement {
  const { Editor } = widgetOf(row.view);

  return (
    <label className="field" data-arg={row.name}>
      <span className="field__label mono">
        {row.name}
        {row.required && <span className="field__required">*</span>}
        {!known && " (extra)"}
      </span>
      {row.description !== "" && <span className="field__hint">{row.description}</span>}
      {row.bound !== "" ? (
        <span className="field__bound">◂ {row.bound}</span>
      ) : (
        <Editor name={row.name} view={row.view} value={row.value} required={row.required} onChange={onValue} />
      )}
      {!row.required && row.value !== undefined && (
        <button type="button" className="btn btn--tiny" onClick={onClear}>
          clear
        </button>
      )}
    </label>
  );
}

/** Форма задачи: имя, инструмент, intent, аргументы виджетами по каталогу, fd-порты. */
export function TaskForm({ task, catalog, edges, taken, onChange, onRename, onRemove, onClose }: Props): ReactElement {
  const facts = catalog[task.tool];
  const [draftName, setDraftName] = useState(task.name);
  const [newArg, setNewArg] = useState("");
  const [newPort, setNewPort] = useState("");

  const rows = blockRows(task, facts, edges);
  const known = new Set((facts?.args ?? []).map((arg) => arg.name));

  const setArg = (name: string, value: unknown): void => {
    if (value === undefined) {
      onChange({ ...task, args: without(task.args, name) });
      return;
    }

    onChange({ ...task, args: { ...task.args, [name]: value } });
  };

  const setPort = (name: string, direction: PortDirection): void => {
    onChange({ ...task, ports: { ...task.ports, [name]: direction } });
  };

  const dropPort = (name: string): void => {
    onChange({ ...task, ports: without(task.ports, name) });
  };

  const commitName = (): void => {
    if (draftName === task.name) {
      return;
    }

    if (!isIdent(draftName) || taken.includes(draftName)) {
      setDraftName(task.name);
      return;
    }

    onRename(draftName);
  };

  return (
    <aside className="inspector form" aria-label="task form">
      <div className="inspector__head">
        <span className="eyebrow">task</span>
        <input
          className="input"
          value={draftName}
          onChange={(event) => {
            setDraftName(event.target.value);
          }}
          onBlur={commitName}
          aria-label="task name"
        />
        <button type="button" className="icon-btn" onClick={onRemove} title="Remove task" aria-label="Remove task">
          <Trash2 size={14} />
        </button>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close inspector">
          <X size={14} />
        </button>
      </div>
      <div className="inspector__body">
        <label className="field">
          <span className="field__label">tool</span>
          <select
            className="input mono"
            value={task.tool}
            onChange={(event) => {
              onChange({ ...task, tool: event.target.value });
            }}
          >
            {Object.values(catalog)
              .filter((tool) => tool.availability === "available" || tool.name === task.tool)
              .map((tool) => (
                <option key={tool.name} value={tool.name}>
                  {tool.name}
                </option>
              ))}
          </select>
          {facts !== undefined && facts.description !== "" && <span className="field__hint">{facts.description}</span>}
        </label>

        <label className="field">
          <span className="field__label">intent</span>
          <input
            className="input"
            value={intentOf(task)}
            placeholder="what this step does"
            onChange={(event) => {
              onChange(withIntent(task, event.target.value));
            }}
            aria-label="task intent"
          />
        </label>

        <h4 className="eyebrow">args</h4>
        {rows.body.map((row) => (
          <ArgField
            key={row.name}
            row={row}
            known={known.has(row.name)}
            onValue={(value) => {
              setArg(row.name, value);
            }}
            onClear={() => {
              setArg(row.name, undefined);
            }}
          />
        ))}
        <div className="field field--row">
          <input
            className="input mono"
            placeholder="new arg"
            value={newArg}
            onChange={(event) => {
              setNewArg(event.target.value);
            }}
            aria-label="new arg"
          />
          <button
            type="button"
            className="btn"
            disabled={!isIdent(newArg) || known.has(newArg) || newArg in task.args}
            onClick={() => {
              setArg(newArg, "");
              setNewArg("");
            }}
          >
            add
          </button>
        </div>

        {(facts?.task_ports ?? false) && (
          <>
            <h4 className="eyebrow">ports</h4>
            {Object.entries(task.ports).map(([name, direction]) => (
              <div className="field field--row" key={name}>
                <span className="mono">{name}</span>
                <select
                  className="input"
                  value={direction}
                  onChange={(event) => {
                    setPort(name, event.target.value === "read" ? "read" : "write");
                  }}
                  aria-label={`port ${name} direction`}
                >
                  <option value="read">read</option>
                  <option value="write">write</option>
                </select>
                <button
                  type="button"
                  className="btn btn--tiny"
                  onClick={() => {
                    dropPort(name);
                  }}
                >
                  drop
                </button>
              </div>
            ))}
            <div className="field field--row">
              <input
                className="input mono"
                placeholder="new port"
                value={newPort}
                onChange={(event) => {
                  setNewPort(event.target.value);
                }}
                aria-label="new port"
              />
              <button
                type="button"
                className="btn"
                disabled={!isIdent(newPort) || newPort in task.ports}
                onClick={() => {
                  setPort(newPort, "write");
                  setNewPort("");
                }}
              >
                add
              </button>
            </div>
          </>
        )}
      </div>
    </aside>
  );
}
