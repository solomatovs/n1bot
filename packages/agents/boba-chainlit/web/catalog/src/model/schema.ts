import { z } from "zod";

/** Подмножество JSON Schema, которое выдаёт pydantic для профиля соединения:
 * объекты, варианты по дискриминатору, nullable через anyOf, скаляры и enum. */

export type JsonSchema = {
  $ref?: string | undefined;
  $defs?: Record<string, JsonSchema> | undefined;
  type?: string | string[] | undefined;
  title?: string | undefined;
  description?: string | undefined;
  format?: string | undefined;
  const?: unknown;
  enum?: unknown[] | undefined;
  default?: unknown;
  properties?: Record<string, JsonSchema> | undefined;
  required?: string[] | undefined;
  additionalProperties?: boolean | JsonSchema | undefined;
  items?: JsonSchema | undefined;
  anyOf?: JsonSchema[] | undefined;
  oneOf?: JsonSchema[] | undefined;
  discriminator?: { propertyName: string; mapping?: Record<string, string> | undefined } | undefined;
};

const JsonSchemaZ: z.ZodType<JsonSchema> = z.lazy(() =>
  z.object({
    $ref: z.string().optional(),
    $defs: z.record(JsonSchemaZ).optional(),
    type: z.union([z.string(), z.array(z.string())]).optional(),
    title: z.string().optional(),
    description: z.string().optional(),
    format: z.string().optional(),
    const: z.unknown().optional(),
    enum: z.array(z.unknown()).optional(),
    default: z.unknown().optional(),
    properties: z.record(JsonSchemaZ).optional(),
    required: z.array(z.string()).optional(),
    additionalProperties: z.union([z.boolean(), JsonSchemaZ]).optional(),
    items: JsonSchemaZ.optional(),
    anyOf: z.array(JsonSchemaZ).optional(),
    oneOf: z.array(JsonSchemaZ).optional(),
    discriminator: z.object({ propertyName: z.string(), mapping: z.record(z.string()).optional() }).optional(),
  }),
);

/** Копия схемы с подписью и описанием ссылки поверх определения. */
function withMeta(base: JsonSchema, over: JsonSchema): JsonSchema {
  const copy: JsonSchema = { ...base };
  if (over.description !== undefined) {
    copy.description = over.description;
  }
  if (over.title !== undefined) {
    copy.title = over.title;
  }

  return copy;
}

export function parseSchema(raw: unknown): JsonSchema {
  return JsonSchemaZ.parse(raw);
}

/** Вариант дискриминированного объединения: значение дискриминатора и его схема. */
export type Variant = {
  tag: string;
  schema: JsonSchema;
};

/** Разобранный узел схемы: что рисовать и чем заполнять. */
export type Node =
  | { kind: "union"; field: string; variants: Variant[]; nullable: boolean }
  | { kind: "object"; properties: [string, JsonSchema][]; required: Set<string>; nullable: boolean }
  | { kind: "map"; nullable: boolean }
  | { kind: "enum"; options: string[]; nullable: boolean }
  | { kind: "const"; value: unknown }
  | { kind: "string"; secret: boolean; nullable: boolean }
  | { kind: "number"; integer: boolean; nullable: boolean }
  | { kind: "boolean"; nullable: boolean }
  | { kind: "lines"; nullable: boolean }
  | { kind: "json"; nullable: boolean };

const REF_PREFIX = "#/$defs/";

/** Схема с корнем и $defs: разрешает ссылки и раскладывает узлы. */
export class SchemaDoc {
  constructor(readonly root: JsonSchema) {}

  resolve(schema: JsonSchema): JsonSchema {
    if (schema.$ref === undefined) {
      return schema;
    }

    if (!schema.$ref.startsWith(REF_PREFIX)) {
      throw new Error(`unsupported $ref ${schema.$ref}`);
    }

    const name = schema.$ref.slice(REF_PREFIX.length);
    const found = this.root.$defs?.[name];
    if (found === undefined) {
      throw new Error(`unknown $def ${name}`);
    }

    // описание ссылки важнее описания определения: оно про это поле
    return withMeta(found, schema);
  }

  node(schema: JsonSchema): Node {
    const resolved = this.resolve(schema);
    const { inner, nullable } = this.unwrapNullable(resolved);

    if (inner.const !== undefined) {
      return { kind: "const", value: inner.const };
    }

    const variants = this.variantsOf(inner);
    if (variants !== null) {
      return { kind: "union", field: variants.field, variants: variants.variants, nullable };
    }

    if (inner.enum !== undefined) {
      return { kind: "enum", options: inner.enum.map(String), nullable };
    }

    const type = this.typeOf(inner);
    if (type === "object" || inner.properties !== undefined) {
      if (inner.properties === undefined) {
        return { kind: "map", nullable };
      }

      return {
        kind: "object",
        properties: Object.entries(inner.properties),
        required: new Set(inner.required ?? []),
        nullable,
      };
    }

    if (type === "string") {
      return { kind: "string", secret: inner.format === "password", nullable };
    }

    if (type === "integer" || type === "number") {
      return { kind: "number", integer: type === "integer", nullable };
    }

    if (type === "boolean") {
      return { kind: "boolean", nullable };
    }

    if (type === "array") {
      const items = inner.items === undefined ? null : this.resolve(inner.items);
      if (items !== null && this.typeOf(items) === "string") {
        return { kind: "lines", nullable };
      }
    }

    return { kind: "json", nullable };
  }

  /** Значение по умолчанию для узла: то, чем заполняется новая форма. */
  defaults(schema: JsonSchema): unknown {
    const resolved = this.resolve(schema);
    if (resolved.default !== undefined) {
      return resolved.default;
    }

    const node = this.node(resolved);
    switch (node.kind) {
      case "const":
        return node.value;
      case "union":
        return this.variantDefaults(this.firstVariant(node), node.field);
      case "object":
        return this.objectDefaults(node);
      case "map":
        return {};
      case "enum":
        return node.nullable ? null : node.options[0];
      case "string":
        return node.nullable ? null : "";
      case "number":
        return node.nullable ? null : 0;
      case "boolean":
        return node.nullable ? null : false;
      case "lines":
        return node.nullable ? null : [];
      case "json":
        return node.nullable ? null : {};
    }
  }

  variantDefaults(variant: Variant, field: string): Record<string, unknown> {
    const built = this.defaults(variant.schema);
    if (typeof built !== "object" || built === null || Array.isArray(built)) {
      return { [field]: variant.tag };
    }

    return { ...(built as Record<string, unknown>), [field]: variant.tag };
  }

  /** Вариант, которому принадлежит значение: по полю дискриминатора. */
  variantOf(node: Extract<Node, { kind: "union" }>, value: unknown): Variant {
    const tag =
      typeof value === "object" && value !== null ? (value as Record<string, unknown>)[node.field] : undefined;
    return node.variants.find((variant) => variant.tag === tag) ?? this.firstVariant(node);
  }

  /** Путь формы по loc ошибки сервера: метки вариантов объединений в путь не входят. */
  formPath(root: string, loc: (string | number)[]): string {
    const parts = [root];
    let current: JsonSchema | null = this.root;
    for (const segment of loc) {
      if (current === null) {
        parts.push(String(segment));
        continue;
      }

      const node = this.node(current);
      if (node.kind === "union") {
        // сегмент — метка варианта: спускаемся в него, в путь не пишем
        current = node.variants.find((variant) => variant.tag === segment)?.schema ?? null;
        continue;
      }

      parts.push(String(segment));
      if (node.kind === "object") {
        current = node.properties.find(([name]) => name === String(segment))?.[1] ?? null;
        continue;
      }

      current = null;
    }

    return parts.join(".");
  }

  private firstVariant(node: Extract<Node, { kind: "union" }>): Variant {
    const first = node.variants[0];
    if (first === undefined) {
      throw new Error(`union by ${node.field} has no variants`);
    }

    return first;
  }

  private objectDefaults(node: Extract<Node, { kind: "object" }>): Record<string, unknown> {
    const built: Record<string, unknown> = {};
    for (const [name, property] of node.properties) {
      built[name] = this.defaults(property);
    }

    return built;
  }

  private unwrapNullable(schema: JsonSchema): { inner: JsonSchema; nullable: boolean } {
    if (schema.anyOf === undefined) {
      return { inner: schema, nullable: false };
    }

    const rest = schema.anyOf.filter((option) => option.type !== "null");
    const nullable = rest.length !== schema.anyOf.length;
    const only = rest[0];
    if (rest.length === 1 && only !== undefined) {
      return { inner: withMeta(this.resolve(only), schema), nullable };
    }

    // несколько типов без дискриминатора (bool | string): редактируется как JSON
    const mixed: JsonSchema = withMeta({ type: "__mixed__" }, schema);
    return { inner: mixed, nullable };
  }

  private variantsOf(schema: JsonSchema): { field: string; variants: Variant[] } | null {
    const options = schema.oneOf ?? (schema.discriminator !== undefined ? schema.anyOf : undefined);
    if (options === undefined || schema.discriminator === undefined) {
      return null;
    }

    const field = schema.discriminator.propertyName;
    const variants: Variant[] = [];
    for (const option of options) {
      const resolved = this.resolve(option);
      const tagSchema = resolved.properties?.[field];
      const tag = tagSchema?.const ?? tagSchema?.enum?.[0];
      if (typeof tag !== "string") {
        throw new Error(`variant without a ${field} literal`);
      }

      variants.push({ tag, schema: option });
    }

    return { field, variants };
  }

  private typeOf(schema: JsonSchema): string | undefined {
    if (Array.isArray(schema.type)) {
      return schema.type.find((type) => type !== "null");
    }

    return schema.type;
  }
}

/** Маска секрета из ответа api: назад её отправлять нельзя, форма показывает пусто. */
export const SECRET_MASK = "**********";

/** Копия значения, где замаскированные секреты заменены пустыми строками. */
export function withoutMaskedSecrets(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(withoutMaskedSecrets);
  }

  if (typeof value === "object" && value !== null) {
    const copy: Record<string, unknown> = {};
    for (const [name, inner] of Object.entries(value as Record<string, unknown>)) {
      copy[name] = withoutMaskedSecrets(inner);
    }

    return copy;
  }

  if (value === SECRET_MASK) {
    return "";
  }

  return value;
}
