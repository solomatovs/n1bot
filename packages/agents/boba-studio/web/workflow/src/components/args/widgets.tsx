import type { ReactElement } from "react";

import { valueText } from "../../model/args";
import { clipText } from "../../model/json";
import type { ArgKind, ArgView } from "../../model/workflow";
import { JsonView } from "../JsonView";

/** Реестр виджетов аргумента по kind: Row — значение строкой в узле,
 * Editor — поле формы. Неизвестный kind сюда не доходит: схема каталога
 * подменяет его текстом. */

const ROW_CLIP = 48;
const CODE_ROWS = 6;
const MASK = "••••••";

export type RowProps = {
  view: ArgView;
  value: unknown;
};

export type EditorProps = {
  name: string;
  view: ArgView;
  value: unknown;
  required: boolean;
  onChange: (value: unknown) => void;
};

export type ArgWidget = {
  Row: (props: RowProps) => ReactElement;
  Editor: (props: EditorProps) => ReactElement;
};

function Line({ value }: RowProps): ReactElement {
  return <span className="arg-row__value">{clipText(valueText(value), ROW_CLIP)}</span>;
}

function CodeRow({ value }: RowProps): ReactElement {
  return <span className="arg-row__value arg-row__value--code">{clipText(valueText(value), ROW_CLIP)}</span>;
}

function BoolRow({ value }: RowProps): ReactElement {
  const on = value === true;
  return (
    <span className="arg-row__value arg-row__value--bool" data-on={on}>
      {value === undefined ? "" : String(on)}
    </span>
  );
}

function NumberRow({ view, value }: RowProps): ReactElement {
  const unit = view.kind === "number" ? view.unit : "";
  return (
    <span className="arg-row__value arg-row__value--number">
      {valueText(value)}
      {value !== undefined && unit !== "" ? ` ${unit}` : ""}
    </span>
  );
}

function SecretRow({ value }: RowProps): ReactElement {
  return <span className="arg-row__value arg-row__value--secret">{value === undefined ? "" : MASK}</span>;
}

function JsonRow({ value }: RowProps): ReactElement {
  if (value === undefined) {
    return <span className="arg-row__value" />;
  }

  if (typeof value !== "object" || value === null) {
    return <Line view={{ kind: "json", placement: "body" }} value={value} />;
  }

  return (
    <span className="arg-row__value arg-row__value--json">
      <JsonView value={value} clip={ROW_CLIP} />
    </span>
  );
}

function TextEditor({ name, view, value, required, onChange }: EditorProps): ReactElement {
  const multiline = view.kind === "text" && view.multiline;
  const placeholder = view.kind === "text" && view.placeholder !== "" ? view.placeholder : hint(required);
  if (multiline) {
    return (
      <textarea
        className="input mono"
        rows={3}
        value={valueText(value)}
        placeholder={placeholder}
        onChange={(event) => {
          onChange(event.target.value);
        }}
        aria-label={`arg ${name}`}
      />
    );
  }

  return (
    <input
      className="input mono"
      value={valueText(value)}
      placeholder={placeholder}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function CodeEditor({ name, view, value, required, onChange }: EditorProps): ReactElement {
  const lang = view.kind === "code" ? view.lang : "";
  return (
    <textarea
      className="input mono input--code"
      rows={CODE_ROWS}
      data-lang={lang}
      spellCheck={false}
      value={valueText(value)}
      placeholder={hint(required)}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function ConnectionEditor({ name, view, value, required, onChange }: EditorProps): ReactElement {
  const family = view.kind === "connection" ? view.family : "";
  return (
    <input
      className="input mono"
      value={valueText(value)}
      placeholder={family === "" ? hint(required) : `${family} connection`}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function EnumEditor({ name, view, value, onChange }: EditorProps): ReactElement {
  const options = view.kind === "enum" ? view.options : [];
  return (
    <select
      className="input mono"
      value={valueText(value)}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      aria-label={`arg ${name}`}
    >
      <option value="">—</option>
      {options.map((option) => (
        <option key={option} value={option}>
          {option}
        </option>
      ))}
    </select>
  );
}

function NumberEditor({ name, view, value, required, onChange }: EditorProps): ReactElement {
  const bounds = view.kind === "number" ? view : null;
  return (
    <input
      className="input mono"
      type="number"
      value={valueText(value)}
      min={bounds?.minimum ?? undefined}
      max={bounds?.maximum ?? undefined}
      placeholder={hint(required)}
      onChange={(event) => {
        onChange(numberOf(event.target.value));
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function BoolEditor({ name, value, onChange }: EditorProps): ReactElement {
  return (
    <label className="toggle">
      <input
        type="checkbox"
        checked={value === true}
        onChange={(event) => {
          onChange(event.target.checked);
        }}
        aria-label={`arg ${name}`}
      />
      <span>{value === true ? "true" : "false"}</span>
    </label>
  );
}

function SecretEditor({ name, value, required, onChange }: EditorProps): ReactElement {
  return (
    <input
      className="input mono"
      type="password"
      value={valueText(value)}
      placeholder={hint(required)}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function JsonEditor({ name, value, required, onChange }: EditorProps): ReactElement {
  return (
    <textarea
      className="input mono"
      rows={4}
      spellCheck={false}
      value={jsonText(value)}
      placeholder={hint(required)}
      onChange={(event) => {
        onChange(jsonOf(event.target.value));
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function hint(required: boolean): string {
  return required ? "required" : "optional";
}

function numberOf(text: string): unknown {
  if (text.trim() === "") {
    return undefined;
  }

  const parsed = Number(text);
  return Number.isNaN(parsed) ? text : parsed;
}

function jsonText(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }

  if (value === undefined) {
    return "";
  }

  return JSON.stringify(value, null, 2);
}

/** Валидный json становится структурой сразу; остальное живёт строкой до правки. */
function jsonOf(text: string): unknown {
  if (text.trim() === "") {
    return undefined;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

const WIDGETS: Record<ArgKind, ArgWidget> = {
  text: { Row: Line, Editor: TextEditor },
  code: { Row: CodeRow, Editor: CodeEditor },
  connection: { Row: Line, Editor: ConnectionEditor },
  enum: { Row: Line, Editor: EnumEditor },
  number: { Row: NumberRow, Editor: NumberEditor },
  bool: { Row: BoolRow, Editor: BoolEditor },
  path: { Row: Line, Editor: TextEditor },
  json: { Row: JsonRow, Editor: JsonEditor },
  secret: { Row: SecretRow, Editor: SecretEditor },
  intent: { Row: Line, Editor: TextEditor },
};

export function widgetOf(view: ArgView): ArgWidget {
  return WIDGETS[view.kind];
}
