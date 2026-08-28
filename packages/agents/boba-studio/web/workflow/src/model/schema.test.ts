import { describe, expect, it } from "vitest";

import { SchemaDoc, parseSchema, withoutMaskedSecrets } from "./schema";

const RAW = {
  discriminator: { propertyName: "kind" },
  oneOf: [{ $ref: "#/$defs/Pg" }, { $ref: "#/$defs/Web" }],
  $defs: {
    Pg: {
      type: "object",
      required: ["auth"],
      properties: {
        kind: { const: "postgres", type: "string" },
        host: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
        auth: {
          discriminator: { propertyName: "method" },
          oneOf: [{ $ref: "#/$defs/Trust" }, { $ref: "#/$defs/Password" }],
        },
        pool: { $ref: "#/$defs/Pool" },
      },
    },
    Trust: { type: "object", required: ["method", "user"], properties: { method: { const: "trust" }, user: { type: "string" } } },
    Password: {
      type: "object",
      required: ["method", "user", "password"],
      properties: { method: { const: "password" }, user: { type: "string" }, password: { type: "string", format: "password" } },
    },
    Pool: { type: "object", properties: { min_size: { type: "integer", default: 1 } } },
    Web: { type: "object", properties: { kind: { const: "web" }, ssl_verify: { type: "boolean", default: true } } },
  },
};

function propertyOf(doc: SchemaDoc, ref: string, name: string) {
  const node = doc.node({ $ref: ref });
  if (node.kind !== "object") throw new Error(node.kind);
  const found = node.properties.find(([property]) => property === name);
  if (found === undefined) throw new Error(`no property ${name}`);
  return found[1];
}

describe("SchemaDoc", () => {
  const doc = new SchemaDoc(parseSchema(RAW));

  it("reads the root as a union by kind", () => {
    const node = doc.node(doc.root);
    expect(node.kind).toBe("union");
    if (node.kind !== "union") return;
    expect(node.field).toBe("kind");
    expect(node.variants.map((v) => v.tag)).toEqual(["postgres", "web"]);
  });

  it("builds defaults through nested unions and nullable fields", () => {
    expect(doc.defaults(doc.root)).toEqual({
      kind: "postgres",
      host: null,
      auth: { method: "trust", user: "" },
      pool: { min_size: 1 },
    });
  });

  it("marks secrets and nullable scalars", () => {
    expect(doc.node(propertyOf(doc, "#/$defs/Password", "password"))).toEqual({ kind: "string", secret: true, nullable: false });
    expect(doc.node(propertyOf(doc, "#/$defs/Pg", "host"))).toEqual({ kind: "string", secret: false, nullable: true });
  });

  it("maps server error locations to form paths without variant tags", () => {
    expect(doc.formPath("profile", ["postgres", "auth", "password", "password"])).toBe("profile.auth.password");
    expect(doc.formPath("profile", ["postgres"])).toBe("profile");
    expect(doc.formPath("profile", ["web", "ssl_verify"])).toBe("profile.ssl_verify");
  });

  it("picks the variant of a value by its discriminator", () => {
    const node = doc.node(doc.root);
    if (node.kind !== "union") throw new Error(node.kind);
    expect(doc.variantOf(node, { kind: "web" }).tag).toBe("web");
    expect(doc.variantOf(node, { kind: "nope" }).tag).toBe("postgres");
  });
});

describe("withoutMaskedSecrets", () => {
  it("blanks masked strings at any depth", () => {
    expect(withoutMaskedSecrets({ a: { b: "**********", c: "keep" }, d: ["**********"] })).toEqual({
      a: { b: "", c: "keep" },
      d: [""],
    });
  });
});
