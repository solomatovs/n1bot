import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

// Дерево JSON ответа инструмента (resource application/json) под выходом
// шага: узлы сворачивает нативный <details>, корень раскрыт, вложенное
// свёрнуто, длинные массивы и объекты раскрываются порциями. Ключи и значения — только текстом React: разметки
// и ссылок из данных сервера в странице нет.
const PAGE = 100;

function kindOf(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function entriesOf(value, kind) {
  if (kind === "array") return value.map((item, index) => [index, item]);
  return Object.entries(value);
}

function Key({ name }) {
  if (name === undefined) return null;

  let shown = JSON.stringify(name);
  if (typeof name === "number") shown = String(name);

  return (
    <span className="text-purple-700 dark:text-purple-300">{shown}: </span>
  );
}

function Scalar({ value }) {
  const kind = kindOf(value);

  if (kind === "string") {
    return (
      <span className="text-green-700 dark:text-green-400 break-all">
        {JSON.stringify(value)}
      </span>
    );
  }

  if (kind === "number" || kind === "boolean") {
    return (
      <span className="text-blue-700 dark:text-blue-400">{String(value)}</span>
    );
  }

  return <span className="text-muted-foreground">null</span>;
}

function Node({ name, value, open }) {
  const [expanded, setExpanded] = useState(open);
  const [shown, setShown] = useState(PAGE);
  const kind = kindOf(value);

  if (kind !== "array" && kind !== "object") {
    return (
      <div className="pl-4" data-json-leaf>
        <Key name={name} />
        <Scalar value={value} />
      </div>
    );
  }

  const entries = entriesOf(value, kind);

  let brackets = ["{", "}"];
  let unit = "key";
  if (kind === "array") {
    brackets = ["[", "]"];
    unit = "item";
  }
  if (entries.length !== 1) unit = `${unit}s`;

  let chevron = <ChevronRight className="h-3 w-3 shrink-0" />;
  if (expanded) chevron = <ChevronDown className="h-3 w-3 shrink-0" />;

  const rest = entries.length - shown;

  return (
    <details
      className="pl-4"
      data-json-node
      open={open}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary className="inline-flex cursor-pointer list-none items-center gap-1">
        {chevron}
        <Key name={name} />
        <span>{brackets[0]}</span>
        {!expanded && (
          <span className="text-muted-foreground">
            {` ${entries.length} ${unit} `}
            {brackets[1]}
          </span>
        )}
      </summary>
      {expanded &&
        entries
          .slice(0, shown)
          .map(([key, item]) => (
            <Node key={key} name={key} value={item} open={false} />
          ))}
      {expanded && rest > 0 && (
        <button
          type="button"
          className="pl-4 text-muted-foreground underline"
          onClick={() => setShown(shown + PAGE)}
        >
          {`show ${Math.min(PAGE, rest)} more of ${rest}`}
        </button>
      )}
      {expanded && <div>{brackets[1]}</div>}
    </details>
  );
}

export default function JsonTree() {
  return (
    <div
      className="font-mono text-sm overflow-x-auto -ml-4 py-1"
      data-json-tree
    >
      <Node value={props.value} open={true} />
    </div>
  );
}
