import { ChevronDown, ChevronRight } from "lucide-react";
import { type ReactElement, useState } from "react";

import { type JsonSchema, type Node, type SchemaDoc } from "../../model/schema";
import { Field, Input, Select, TextArea } from "../../ui";

type NodeProps = {
  doc: SchemaDoc;
  schema: JsonSchema;
  value: unknown;
  path: string;
  label: string;
  required: boolean;
  readonly: boolean;
  /** Ошибки сервера по пути поля: показываются под полем. */
  issues: ReadonlyMap<string, string>;
  onChange: (value: unknown) => void;
};

function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }

  return {};
}

function asString(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }

  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }

  return "";
}

/** Поле-скаляр: строка, секрет, число, флаг, enum, список строк, JSON. */
function Scalar({ node, schema, value, path, label, required, readonly, issues, onChange }: NodeProps & { node: Node }): ReactElement {
  const issue = issues.get(path);
  const control = ((): ReactElement => {
    switch (node.kind) {
      case "enum":
        return (
          <Select
            aria-label={path}
            value={asString(value)}
            disabled={readonly}
            onChange={(event) => {
              onChange(event.target.value === "" ? null : event.target.value);
            }}
          >
            {node.nullable && <option value="">—</option>}
            {node.options.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </Select>
        );
      case "boolean":
        return (
          <Input
            type="checkbox"
            aria-label={path}
            checked={value === true}
            disabled={readonly}
            onChange={(event) => {
              onChange(event.target.checked);
            }}
          />
        );
      case "number":
        return (
          <Input
            type="number"
            step={node.integer ? 1 : "any"}
            aria-label={path}
            value={asString(value)}
            disabled={readonly}
            onChange={(event) => {
              const text = event.target.value;
              onChange(text === "" ? null : Number(text));
            }}
          />
        );
      case "lines":
        return (
          <TextArea
            aria-label={path}
            rows={3}
            value={Array.isArray(value) ? value.map(String).join("\n") : ""}
            disabled={readonly}
            onChange={(event) => {
              const lines = event.target.value.split("\n").filter((line) => line !== "");
              onChange(lines);
            }}
          />
        );
      case "json":
      case "map":
        return (
          <TextArea
            code
            aria-label={path}
            rows={3}
            defaultValue={value === null || value === undefined ? "" : JSON.stringify(value, null, 2)}
            disabled={readonly}
            onBlur={(event) => {
              const text = event.target.value.trim();
              if (text === "") {
                onChange(node.nullable ? null : {});
                return;
              }

              try {
                onChange(JSON.parse(text));
              } catch {
                // невалидный JSON остаётся в поле; сервер отвергнет отправку
              }
            }}
          />
        );
      default:
        return (
          <Input
            type={node.kind === "string" && node.secret ? "password" : "text"}
            aria-label={path}
            value={asString(value)}
            disabled={readonly}
            autoComplete="off"
            onChange={(event) => {
              const text = event.target.value;
              onChange(text === "" && node.kind === "string" && node.nullable ? null : text);
            }}
          />
        );
    }
  })();

  const row = node.kind === "boolean";
  return (
    <Field
      label={label}
      required={required}
      row={row}
      controlFirst={row}
      invalid={issue !== undefined}
      issue={issue}
      dataPath={path}
      hint={schema.description}
    >
      {control}
    </Field>
  );
}

/** Узел схемы: объект — вложенный блок, объединение — пикер варианта, иначе поле. */
export function SchemaNode(props: NodeProps): ReactElement | null {
  const { doc, schema, value, path, label, readonly, onChange } = props;
  const node = doc.node(schema);

  if (node.kind === "const") {
    return null;
  }

  if (node.kind === "union") {
    const variant = doc.variantOf(node, value);
    return (
      <fieldset className="schema-block" data-path={path}>
        <legend className="schema-block__legend">{label}</legend>
        <Field label={node.field} hint={doc.resolve(schema).description}>
          <Select
            aria-label={`${path}.${node.field}`}
            value={variant.tag}
            disabled={readonly}
            onChange={(event) => {
              const picked = node.variants.find((option) => option.tag === event.target.value);
              if (picked !== undefined) {
                onChange(doc.variantDefaults(picked, node.field));
              }
            }}
          >
            {node.variants.map((option) => (
              <option key={option.tag} value={option.tag}>
                {option.tag}
              </option>
            ))}
          </Select>
        </Field>
        <ObjectFields {...props} schema={variant.schema} value={asRecord(value)} />
      </fieldset>
    );
  }

  if (node.kind === "object") {
    return (
      <Block {...props} node={node} />
    );
  }

  return <Scalar {...props} node={node} />;
}

/** Вложенный объект: блок, который сворачивается, если все его поля по умолчанию. */
function Block({ node, ...props }: NodeProps & { node: Extract<Node, { kind: "object" }> }): ReactElement {
  const { doc, schema, value, path, label } = props;
  const untouched = JSON.stringify(value ?? null) === JSON.stringify(doc.defaults(schema));
  const [open, setOpen] = useState(!untouched);

  return (
    <fieldset className="schema-block" data-path={path}>
      <legend className="schema-block__legend">
        <button
          type="button"
          className="schema-block__toggle"
          aria-expanded={open}
          onClick={() => {
            setOpen((current) => !current);
          }}
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          {label}
        </button>
      </legend>
      {open && <ObjectFields {...props} value={asRecord(value)} node={node} />}
    </fieldset>
  );
}

/** Поля объекта: обязательные и заполненные раньше nullable-необязательных. */
function ObjectFields(props: NodeProps & { node?: Extract<Node, { kind: "object" }> }): ReactElement {
  const { doc, schema, value, path, readonly, onChange } = props;
  const node = props.node ?? doc.node(schema);
  if (node.kind !== "object") {
    return <></>;
  }

  const current = asRecord(value);
  const ordered = [...node.properties].sort(([nameA, a], [nameB, b]) => {
    const rank = (name: string, property: JsonSchema): number => {
      if (node.required.has(name)) return 0;
      const inner = doc.node(property);
      if ("nullable" in inner && inner.nullable) return 2;
      return 1;
    };
    return rank(nameA, a) - rank(nameB, b);
  });

  return (
    <>
      {ordered.map(([name, property]) => (
        <SchemaNode
          key={name}
          doc={doc}
          schema={property}
          value={current[name]}
          path={path === "" ? name : `${path}.${name}`}
          label={name}
          required={node.required.has(name)}
          readonly={readonly}
          issues={props.issues}
          onChange={(inner) => {
            onChange({ ...current, [name]: inner });
          }}
        />
      ))}
    </>
  );
}
