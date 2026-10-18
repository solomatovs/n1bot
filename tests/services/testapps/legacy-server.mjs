// MCP-сервер редакции 2025-11-25 на официальном TypeScript SDK 1.x: сосед
// тестов клиента boba, который о boba не знает. Повторяет стандартный сервер
// тестов клиента (standard_server.py) теми запросами и ответами, что есть в
// 2025-11-25, и добавляет то, чего нет у примеров MCP Apps: сессии HTTP с
// забыванием по команде (forget), вопрос пользователю только клиенту с
// elicitation (ask), смену списка инструментов уведомлением (toggle_extra),
// инструмент только для задач (slow_report) и эхо личности клиента и _meta
// запроса (client). Состояние — своё у каждой сессии. Редакцию сервера задают
// параметры: --handshake — версии рукопожатия initialize, которые он знает
// (по умолчанию все версии SDK; пусто — рукопожатия нет), --modern — версии
// с конвертом _meta, которые он называет отказом UnsupportedProtocolVersionError
// на server/discover, как сервер более поздней редакции.
//
// node legacy-server.mjs <порт> [--handshake v,...] [--modern v,...] — HTTP с сессиями на /mcp
// node legacy-server.mjs --stdio                                    — stdio

import { randomUUID } from "node:crypto";
import http from "node:http";
import { parseArgs } from "node:util";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  CompleteRequestSchema,
  ErrorCode,
  GetPromptRequestSchema,
  isInitializeRequest,
  ListPromptsRequestSchema,
  ListResourcesRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListToolsRequestSchema,
  McpError,
  ReadResourceRequestSchema,
  SUPPORTED_PROTOCOL_VERSIONS,
} from "@modelcontextprotocol/sdk/types.js";

const { values: EDITION, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    stdio: { type: "boolean", default: false },
    handshake: { type: "string", default: SUPPORTED_PROTOCOL_VERSIONS.join(",") },
    modern: { type: "string", default: "" },
  },
});
const versions = (listed) => listed.split(",").filter((version) => version !== "");
const HANDSHAKE = versions(EDITION.handshake);
const MODERN = versions(EDITION.modern);
const UNSUPPORTED_VERSION = -32022;

// Рукопожатие редакции сервера: запрошенная версия, если сервер её знает,
// иначе его самая новая — как велит lifecycle 2025-11-25.
class EditionServer extends Server {
  async _oninitialize(request) {
    const result = await super._oninitialize(request);
    const requested = request.params.protocolVersion;
    if (HANDSHAKE.includes(requested)) {
      return { ...result, protocolVersion: requested };
    }

    return { ...result, protocolVersion: HANDSHAKE[0] };
  }
}

const INFO = { name: "legacy-standard", version: "1.0" };
const INSTRUCTIONS = "Call add for sums; read standard://notes first.";
const UI = "io.modelcontextprotocol/ui";
const APP_MIME = "text/html;profile=mcp-app";
const ORDERS = "ui://standard/orders.html";
const ORDERS_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>orders</title></head>
<body><p id="state">waiting for the host</p>
<script>
window.parent.postMessage({ jsonrpc: "2.0", id: 1, method: "ui/initialize", params: {} }, "*");
</script></body></html>`;
const NOTES = "standard://notes";
const LOGO = "standard://logo.png";
const ITEMS = "standard://items/{item}";
const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(16)]);
const RESOURCE_NOT_FOUND = -32002;
const COMPLETIONS = {
  "ref/prompt style": ["plain", "poem", "pirate"],
  "ref/resource item": ["apple", "apricot", "banana"],
};
const TARGET_FORM = {
  type: "object",
  properties: { table: { type: "string", title: "Target", description: "Which table should receive the rows?" } },
  required: ["table"],
};

const object = (properties, required) => ({ type: "object", properties, required });
const text = (value) => ({ content: [{ type: "text", text: String(value) }] });
const ORDER = object(
  { id: { type: "integer" }, customer: { type: "string" }, status: { type: "string" }, total: { type: "number" } },
  ["id", "customer", "status", "total"],
);

const TOOLS = [
  { name: "add", description: "Adds two numbers.", inputSchema: object({ a: { type: "integer" }, b: { type: "integer" } }, ["a", "b"]), annotations: { readOnlyHint: true } },
  { name: "shout", description: "Returns the text in upper case.", inputSchema: object({ text: { type: "string" } }, ["text"]), annotations: { readOnlyHint: false, destructiveHint: false } },
  { name: "broken", description: "Always fails.", inputSchema: object({}, []) },
  { name: "picture", description: "Returns a tiny image.", inputSchema: object({}, []) },
  { name: "blocks", description: "Answers with blocks for the user and the model, or only with structured content.", inputSchema: object({ empty: { type: "boolean" } }, ["empty"]) },
  { name: "sleep_for", description: "Answers after the given delay.", inputSchema: object({ seconds: { type: "number" } }, ["seconds"]) },
  { name: "large", description: "Returns a text of exactly the given size.", inputSchema: object({ size: { type: "integer" } }, ["size"]) },
  { name: "progress", description: "Reports progress once per step, then answers.", inputSchema: object({ steps: { type: "integer" } }, ["steps"]), annotations: { readOnlyHint: true } },
  { name: "toggle_extra", description: "Adds or removes the tool extra and notifies the client.", inputSchema: object({}, []) },
  { name: "ask", description: "Asks the user for the target table when the client declares elicitation, or refuses with an MCP error.", inputSchema: object({ refuse: { type: "boolean" } }, ["refuse"]) },
  { name: "client", description: "Returns the clientInfo and capabilities of the session and the _meta of the request as JSON.", inputSchema: object({}, []) },
  { name: "forget", description: "Forgets the session of the client after the answer, as a restarted server would.", inputSchema: object({}, []) },
  { name: "slow_report", description: "Builds a report; runs only as a task.", inputSchema: object({}, []), execution: { taskSupport: "required" } },
  { name: "orders", description: "Shows the given orders in a table the user can filter.", inputSchema: object({ rows: { type: "array", items: ORDER } }, ["rows"]), _meta: { ui: { resourceUri: ORDERS } } },
  { name: "refresh_orders", description: "Refreshes the orders; only their app calls it.", inputSchema: object({}, []), _meta: { ui: { resourceUri: ORDERS, visibility: ["app"] } } },
  { name: "archive_orders", description: "Archives the orders; only the model calls it.", inputSchema: object({}, []), _meta: { ui: { resourceUri: ORDERS, visibility: ["model"] } } },
];
const EXTRA = { name: "extra", description: "Appears only after toggle_extra.", inputSchema: object({}, []) };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function blocks(empty) {
  if (empty) {
    return { content: [], structuredContent: { rows: 2 } };
  }

  const document = { rows: [{ id: 1, name: "<script>window.__boba_pwned=1</script>" }], total: 1 };

  return {
    content: [
      { type: "text", text: "for the user", annotations: { audience: ["user"] } },
      { type: "text", text: "for the model", annotations: { audience: ["user", "assistant"] } },
      { type: "resource_link", name: "report.csv", uri: "file:///report.csv", mimeType: "text/csv" },
      { type: "resource", resource: { uri: "file:///notes.txt", mimeType: "text/plain", text: "notes" } },
      {
        type: "resource",
        resource: { uri: "file:///answer.json", mimeType: "application/json", text: JSON.stringify(document) },
        annotations: { audience: ["user"] },
      },
    ],
  };
}

function orders(server, rows) {
  const extensions = server.getClientCapabilities()?.extensions ?? {};
  if (UI in extensions) {
    return text(`${rows.length} orders are shown by their app`);
  }

  const lines = [`${rows.length} orders:`];
  for (const row of rows) {
    lines.push(`#${row.id} ${row.customer} ${row.status} ${row.total.toFixed(2)}`);
  }

  return text(lines.join("\n"));
}

async function ask(server, refuse) {
  if (refuse) {
    throw new McpError(ErrorCode.InvalidParams, "ask refuses the request", { reason: "the stand refuses" });
  }

  if (!server.getClientCapabilities()?.elicitation) {
    return text("nobody is asked: the client declares no elicitation");
  }

  const answer = await server.elicitInput({
    mode: "form",
    message: "ask needs the target table.",
    requestedSchema: TARGET_FORM,
  });

  return text(`the user answered ${answer.action}`);
}

async function progress(extra, steps) {
  const token = extra._meta?.progressToken;
  for (let step = 1; step <= steps; step += 1) {
    if (token !== undefined) {
      await extra.sendNotification({
        method: "notifications/progress",
        params: { progressToken: token, progress: step, total: steps, message: `step ${step} of ${steps}` },
      });
    }

    await sleep(10);
  }

  return text(`did ${steps} steps`);
}

function read(uri) {
  if (uri === NOTES) {
    return { contents: [{ uri, mimeType: "text/plain", text: "the notes of the standard server" }] };
  }

  if (uri === LOGO) {
    return { contents: [{ uri, mimeType: "image/png", blob: PNG.toString("base64") }] };
  }

  if (uri === ORDERS) {
    const csp = { resourceDomains: ["https://cdn.example.com"] };
    return { contents: [{ uri, mimeType: APP_MIME, text: ORDERS_HTML, _meta: { ui: { csp } } }] };
  }

  const item = /^standard:\/\/items\/([^/]+)$/.exec(uri);
  if (item !== null) {
    return { contents: [{ uri, mimeType: "text/plain", text: `item ${item[1]}` }] };
  }

  throw new McpError(RESOURCE_NOT_FOUND, `resource ${uri} is not on the server`, { uri });
}

// Сервер одной сессии: forget зовётся после ответа на одноимённый вызов.
function build(forget) {
  const server = new EditionServer(INFO, {
    capabilities: { tools: { listChanged: true }, resources: {}, prompts: { listChanged: false }, completions: {} },
    instructions: INSTRUCTIONS,
  });
  let extraOn = false;

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    if (extraOn) {
      return { tools: [...TOOLS, EXTRA] };
    }

    return { tools: TOOLS };
  });

  server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    const args = request.params.arguments ?? {};
    switch (request.params.name) {
      case "add":
        return text(args.a + args.b);
      case "shout":
        return text(String(args.text).toUpperCase());
      case "broken":
        return { content: [{ type: "text", text: "no such table: orders" }], isError: true };
      case "picture":
        return { content: [{ type: "image", data: PNG.toString("base64"), mimeType: "image/png" }] };
      case "blocks":
        return blocks(args.empty);
      case "sleep_for":
        await sleep(args.seconds * 1000);
        return text(`slept ${args.seconds}`);
      case "large":
        return text("0123456789".repeat(Math.floor(args.size / 10) + 1).slice(0, args.size));
      case "progress":
        return progress(extra, args.steps);
      case "toggle_extra":
        extraOn = !extraOn;
        await server.sendToolListChanged();
        return text(`extra on: ${extraOn ? "True" : "False"}`);
      case "extra":
        return text("extra answered");
      case "ask":
        return ask(server, args.refuse);
      case "client": {
        const seen = { info: server.getClientVersion(), capabilities: server.getClientCapabilities(), meta: request.params._meta ?? {} };
        return text(JSON.stringify(seen));
      }
      case "forget":
        setTimeout(forget, 100);
        return text("forgotten");
      case "slow_report":
        throw new McpError(ErrorCode.MethodNotFound, "slow_report runs only as a task: send tools/call with params.task");
      case "orders":
        return orders(server, args.rows);
      case "refresh_orders":
        return text("orders refreshed");
      case "archive_orders":
        return text("orders archived");
      default:
        throw new McpError(ErrorCode.InvalidParams, `no tool ${request.params.name}`);
    }
  });

  server.setRequestHandler(ListResourcesRequestSchema, async () => ({
    resources: [
      { uri: NOTES, name: "notes", mimeType: "text/plain" },
      { uri: LOGO, name: "logo", mimeType: "image/png" },
      { uri: ORDERS, name: "orders app", mimeType: APP_MIME },
    ],
  }));

  server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
    resourceTemplates: [{ uriTemplate: ITEMS, name: "item", mimeType: "text/plain" }],
  }));

  server.setRequestHandler(ReadResourceRequestSchema, async (request) => read(request.params.uri));

  server.setRequestHandler(ListPromptsRequestSchema, async () => ({
    prompts: [
      {
        name: "greet",
        description: "Greets a person in the given style.",
        arguments: [{ name: "name", required: true }, { name: "style", required: false }],
      },
    ],
  }));

  server.setRequestHandler(GetPromptRequestSchema, async (request) => {
    const name = request.params.arguments?.name;
    const style = request.params.arguments?.style ?? "plain";
    return {
      messages: [
        { role: "user", content: { type: "text", text: `Greet ${name} in the ${style} style.` } },
        { role: "assistant", content: { type: "text", text: `Hello, ${name}!` } },
      ],
    };
  });

  server.setRequestHandler(CompleteRequestSchema, async (request) => {
    const known = COMPLETIONS[`${request.params.ref.type} ${request.params.argument.name}`] ?? [];
    const values = known.filter((value) => value.startsWith(request.params.argument.value));
    return { completion: { values, total: values.length, hasMore: false } };
  });

  return server;
}

function refuse(res, status, code, message, data) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify({ jsonrpc: "2.0", error: { code, message, data }, id: null }));
}

// Отказ сервера более поздней редакции: server/discover — всегда (своих версий
// с конвертом клиент не знает), initialize — если рукопожатия у сервера нет.
function unsupported(res, message) {
  const discovering = message.method === "server/discover";
  const handshaking = isInitializeRequest(message) && HANDSHAKE.length === 0;
  if (!discovering && !handshaking) {
    return false;
  }

  const supported = [...MODERN, ...HANDSHAKE];
  const meta = message.params?._meta ?? {};
  const requested = message.params?.protocolVersion ?? meta["io.modelcontextprotocol/protocolVersion"];
  refuse(res, 400, UNSUPPORTED_VERSION, "Unsupported protocol version", { supported, requested });

  return true;
}

async function body(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }

  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

// Сессии HTTP по Mcp-Session-Id; забытая или чужая сессия — 404, как велит 2025-11-25.
function serveHttp(port) {
  const sessions = new Map();

  const opened = async (req, res) => {
    const id = req.headers["mcp-session-id"];
    if (id !== undefined) {
      const transport = sessions.get(id);
      if (transport === undefined) {
        refuse(res, 404, -32001, "Session not found");
        return;
      }

      await transport.handleRequest(req, res);
      return;
    }

    if (req.method !== "POST") {
      refuse(res, 400, -32000, "Bad Request: Mcp-Session-Id header is required");
      return;
    }

    const message = await body(req);
    if (MODERN.length > 0 && unsupported(res, message)) {
      return;
    }

    if (!isInitializeRequest(message)) {
      refuse(res, 400, -32000, "Bad Request: No valid session ID provided");
      return;
    }

    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (sid) => sessions.set(sid, transport),
    });
    const forget = () => {
      sessions.delete(transport.sessionId);
      transport.close();
    };
    transport.onclose = () => sessions.delete(transport.sessionId);
    await build(forget).connect(transport);
    await transport.handleRequest(req, res, message);
  };

  http
    .createServer((req, res) => {
      opened(req, res).catch((error) => {
        console.error("legacy server:", error);
        if (!res.headersSent) {
          refuse(res, 500, -32603, `legacy server failed: ${error}`);
        }
      });
    })
    .listen(port, "0.0.0.0", () => console.log(`legacy MCP server on http://0.0.0.0:${port}/mcp`));
}

if (EDITION.stdio) {
  await build(() => {}).connect(new StdioServerTransport());
} else {
  serveHttp(Number.parseInt(positionals[0], 10));
}
