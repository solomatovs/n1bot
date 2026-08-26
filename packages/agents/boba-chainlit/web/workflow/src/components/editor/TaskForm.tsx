import { Trash2 } from "lucide-react";
import { type ReactElement, useState } from "react";

import { isIdent, type EditableTask } from "../../model/spec";
import type { PortDirection, ToolCatalog } from "../../model/workflow";

type Props = {
  task: EditableTask;
  catalog: ToolCatalog;
  taken: string[];
  onChange: (task: EditableTask) => void;
  onRename: (to: string) => void;
  onRemove: () => void;
};

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([name]) => name !== key));
}

function textOf(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }

  if (value === undefined) {
    return "";
  }

  return JSON.stringify(value);
}

/** Форма задачи: имя, инструмент, аргументы по каталогу, fd-порты. */
export function TaskForm({ task, catalog, taken, onChange, onRename, onRemove }: Props): ReactElement {
  const facts = catalog[task.tool];
  const [draftName, setDraftName] = useState(task.name);
  const [newArg, setNewArg] = useState("");
  const [newPort, setNewPort] = useState("");

  const argNames = new Set<string>();
  for (const arg of facts?.args ?? []) {
    argNames.add(arg.name);
  }
  for (const name of Object.keys(task.args)) {
    argNames.add(name);
  }
  const required = new Set((facts?.args ?? []).filter((arg) => arg.required).map((arg) => arg.name));

  const setArg = (name: string, value: string): void => {
    onChange({ ...task, args: { ...task.args, [name]: value } });
  };

  const dropArg = (name: string): void => {
    onChange({ ...task, args: without(task.args, name) });
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
    <aside className="inspector task-form">
      <h3 className="inspector__title">
        <input
          className="input"
          value={draftName}
          onChange={(event) => {
            setDraftName(event.target.value);
          }}
          onBlur={commitName}
          aria-label="task name"
        />
        <button type="button" className="btn btn--danger" onClick={onRemove} title="Remove task">
          <Trash2 size={14} />
        </button>
      </h3>
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
      </label>

      <h4>args</h4>
      {[...argNames].map((name) => (
        <label className="field" key={name}>
          <span className="field__label mono">
            {name}
            {required.has(name) && <span className="field__required">*</span>}
            {!argNames.has(name) || facts?.args.some((arg) => arg.name === name) ? null : " (extra)"}
          </span>
          <textarea
            className="input mono"
            rows={2}
            value={textOf(task.args[name])}
            placeholder={required.has(name) ? "required" : "optional"}
            onChange={(event) => {
              setArg(name, event.target.value);
            }}
            aria-label={`arg ${name}`}
          />
          {!required.has(name) && name in task.args && (
            <button type="button" className="btn btn--tiny" onClick={() => { dropArg(name); }}>
              clear
            </button>
          )}
        </label>
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
          disabled={!isIdent(newArg) || argNames.has(newArg)}
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
          <h4>ports</h4>
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
              <button type="button" className="btn btn--tiny" onClick={() => { dropPort(name); }}>
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
    </aside>
  );
}
