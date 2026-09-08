import type { ReactElement } from "react";

import { valueText } from "../../model/args";
import { clipText } from "../../model/json";
import type { ArgRow } from "../../model/args";
import type { EditorKind, FieldEditor, ToolResult } from "../../model/workflow";
import { JsonView } from "../JsonView";
import { Input, Select, TextArea } from "../../ui";

/** Реестр виджетов аргумента по редактору: Row — значение строкой в узле,
 * Editor — поле формы. Аргумент с объявленным показом markdown с языком
 * редактируется как код. Неизвестный редактор сюда не доходит: схема
 * каталога подменяет его текстовым. */

const ROW_CLIP = 48;
const CODE_ROWS = 6;
const MASK = "••••••";

export type RowProps = {
  editor: FieldEditor;
  display: ToolResult | null;
  value: unknown;
};

export type EditorProps = {
  name: string;
  editor: FieldEditor;
  display: ToolResult | null;
  value: unknown;
  required: boolean;
  onChange: (value: unknown) => void;
};

export type ArgWidget = {
  Row: (props: RowProps) => ReactElement;
  Editor: (props: EditorProps) => ReactElement;
};

function Line({ value }: RowProps): ReactElement {
  return (
    <span className="arg-row__value">
      {clipText(valueText(value), ROW_CLIP)}
    </span>
  );
}

function CodeRow({ value }: RowProps): ReactElement {
  return (
    <span className="arg-row__value arg-row__value--code">
      {clipText(valueText(value), ROW_CLIP)}
    </span>
  );
}

function BoolRow({ value }: RowProps): ReactElement {
  const on = value === true;
  return (
    <span className="arg-row__value arg-row__value--bool" data-on={on}>
      {value === undefined ? "" : String(on)}
    </span>
  );
}

function NumberRow({ value }: RowProps): ReactElement {
  return <span className="arg-row__value arg-row__value--number">{valueText(value)}</span>;
}

function SecretRow({ value }: RowProps): ReactElement {
  return (
    <span className="arg-row__value arg-row__value--secret">
      {value === undefined ? "" : MASK}
    </span>
  );
}

function JsonRow({ value }: RowProps): ReactElement {
  if (value === undefined) {
    return <span className="arg-row__value" />;
  }

  if (typeof value !== "object" || value === null) {
    return <Line editor={{ editor: "json" }} display={null} value={value} />;
  }

  return (
    <span className="arg-row__value arg-row__value--json">
      <JsonView value={value} clip={ROW_CLIP} />
    </span>
  );
}

function TextEditor({
  name,
  editor,
  value,
  required,
  onChange,
}: EditorProps): ReactElement {
  const multiline = editor.editor === "text" && editor.multiline;
  const placeholder =
    editor.editor === "text" && editor.placeholder !== ""
      ? editor.placeholder
      : hint(required);
  if (multiline) {
    return (
      <TextArea
        mono
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
    <Input
      mono
      value={valueText(value)}
      placeholder={placeholder}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function CodeEditor({
  name,
  display,
  value,
  required,
  onChange,
}: EditorProps): ReactElement {
  const lang = codeLanguage(display);
  return (
    <TextArea
      mono
      code
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

function ConnectionEditor({
  name,
  editor,
  value,
  required,
  onChange,
}: EditorProps): ReactElement {
  const family = editor.editor === "connection" ? editor.family : "";
  return (
    <Input
      mono
      value={valueText(value)}
      placeholder={family === "" ? hint(required) : `${family} connection`}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      aria-label={`arg ${name}`}
    />
  );
}

function SelectEditor({
  name,
  editor,
  value,
  onChange,
}: EditorProps): ReactElement {
  const options = editor.editor === "select" ? editor.options : [];
  return (
    <Select
      mono
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
    </Select>
  );
}

function NumberEditor({
  name,
  editor,
  value,
  required,
  onChange,
}: EditorProps): ReactElement {
  const bounds = editor.editor === "number" ? editor : null;
  return (
    <Input
      mono
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

function SecretEditor({
  name,
  value,
  required,
  onChange,
}: EditorProps): ReactElement {
  return (
    <Input
      mono
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

function JsonEditor({
  name,
  value,
  required,
  onChange,
}: EditorProps): ReactElement {
  return (
    <TextArea
      mono
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

const WIDGETS: Record<EditorKind, ArgWidget> = {
  text: { Row: Line, Editor: TextEditor },
  connection: { Row: Line, Editor: ConnectionEditor },
  select: { Row: Line, Editor: SelectEditor },
  number: { Row: NumberRow, Editor: NumberEditor },
  bool: { Row: BoolRow, Editor: BoolEditor },
  json: { Row: JsonRow, Editor: JsonEditor },
  secret: { Row: SecretRow, Editor: SecretEditor },
};

const CODE: ArgWidget = { Row: CodeRow, Editor: CodeEditor };

/** Язык объявленного показа markdown; пусто — показ не код. */
export function codeLanguage(display: ToolResult | null): string {
  if (display?.kind !== "markdown") {
    return "";
  }

  const language = display.language;
  if (typeof language !== "string") {
    return "";
  }

  return language;
}

export function widgetOf(row: Pick<ArgRow, "editor" | "display">): ArgWidget {
  if (codeLanguage(row.display) !== "") {
    return CODE;
  }

  return WIDGETS[row.editor.editor];
}
