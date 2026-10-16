import { useEffect, useMemo, useState } from "react";
import { AppRenderer } from "@mcp-ui/client";

// Приложение MCP Apps у шага вызова: AppRenderer рисует его в песочнице на
// хосте сервера (sandbox_url), запросы приложения уходят действиями chainlit в
// серверную часть чата, та ходит на сервер от имени пользователя вкладки.

const browser = {
  get win() {
    if (typeof window === "undefined") return null;
    return window;
  },
  root() {
    if (typeof document === "undefined") return null;
    return document.documentElement;
  },
  origin() {
    const win = this.win;
    if (!win) return "";
    return win.location.origin;
  },
  language() {
    const win = this.win;
    if (!win) return "en";
    return win.navigator.language;
  },
};

const APP_MIME = "text/html;profile=mcp-app";
const METHOD_NOT_FOUND = -32601;
const HOST_INFO = { name: "boba-aid", version: "1" };
const HOST_CAPABILITIES = {
  openLinks: {},
  serverTools: {},
  serverResources: {},
  message: { text: {} },
  updateModelContext: { text: {}, structuredContent: {} },
};

function originOf(address) {
  try {
    return new URL(address).origin;
  } catch (error) {
    return "";
  }
}

// ошибка JSON-RPC приложению: код уходит в ответ моста, текст — в message
function failure(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

async function ask(name, payload) {
  const answer = await callAction({ name, payload });
  const response = answer && answer.response;
  if (!response) {
    throw new Error(`${name}: the chat gave no answer to the app request`);
  }
  if (response.error) {
    throw failure(response.error.code, response.error.message);
  }
  return response;
}

// HTML и CSP ресурса: AppRenderer сам _meta.ui.csp не читает; домены чата
// из CSP выбрасываются — источник чата приложению не открывается
function appOf(read, chat) {
  const contents = (read && read.contents) || [];
  if (contents.length !== 1) {
    throw new Error(`the app resource has ${contents.length} contents, expected one`);
  }
  const resource = contents[0];
  if (resource.mimeType !== APP_MIME) {
    throw new Error(`the app resource is ${resource.mimeType}, expected ${APP_MIME}`);
  }

  let html = resource.text;
  if (typeof html !== "string") html = atob(resource.blob || "");

  const meta = (resource._meta && resource._meta.ui) || {};
  let csp;
  if (meta.csp) {
    csp = {};
    for (const [key, domains] of Object.entries(meta.csp)) {
      if (!Array.isArray(domains)) continue;
      csp[key] = domains.filter((domain) => originOf(domain) !== chat);
    }
  }
  return { html, csp };
}

function textOf(content) {
  const parts = [];
  for (const block of content || []) {
    if (block.type === "text") parts.push(block.text);
  }
  return parts.join("\n");
}

function useDark() {
  const [dark, setDark] = useState(
    () => browser.root()?.classList.contains("dark") || false
  );
  useEffect(() => {
    const root = browser.root();
    if (!root) return undefined;
    const observer = new MutationObserver(() =>
      setDark(root.classList.contains("dark"))
    );
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return dark;
}

export default function ToolApp() {
  const chat = browser.origin();
  const isolated = originOf(props.sandbox_url) !== chat;
  const dark = useDark();
  const [app, setApp] = useState(null);
  const [error, setError] = useState(null);

  const request = (params) => ({
    server: props.server,
    call_id: props.call_id,
    params,
  });

  useEffect(() => {
    if (!isolated) return undefined;
    let cancelled = false;
    ask("mcp_app_read", request({ uri: props.resource_uri }))
      .then((read) => {
        if (!cancelled) setApp(appOf(read, chat));
      })
      .catch((failed) => {
        if (!cancelled) setError(failed.message);
      });
    return () => {
      cancelled = true;
    };
  }, [props.server, props.resource_uri, isolated]);

  const sandbox = useMemo(() => {
    if (!app) return null;
    return { url: new URL(props.sandbox_url), csp: app.csp };
  }, [props.sandbox_url, app]);

  const hostContext = useMemo(
    () => ({
      theme: dark ? "dark" : "light",
      displayMode: "inline",
      availableDisplayModes: ["inline"],
      platform: "web",
      locale: browser.language(),
    }),
    [dark]
  );

  if (!isolated) {
    return (
      <div className="text-sm text-destructive" data-mcp-app-error>
        The app sandbox {props.sandbox_url} is on the origin of the chat; the
        app is not drawn.
      </div>
    );
  }
  if (error) {
    return (
      <div className="text-sm text-destructive" data-mcp-app-error>
        {error}
      </div>
    );
  }
  if (!app) return null;

  return (
    <div className="w-full max-w-full overflow-hidden" data-mcp-app={props.call_id}>
      <AppRenderer
        toolName={props.tool}
        html={app.html}
        sandbox={sandbox}
        toolInput={props.input}
        toolResult={props.result}
        hostInfo={HOST_INFO}
        hostContext={hostContext}
        hostCapabilities={HOST_CAPABILITIES}
        onCallTool={(params) => ask("mcp_app_call", request(params))}
        onReadResource={(params) => ask("mcp_app_read", request(params))}
        onMessage={async (params) => {
          const text = textOf(params.content);
          if (!text) throw failure(-32602, "ui/message carries no text");
          await sendUserMessage(text);
          return {};
        }}
        onOpenLink={async ({ url }) => {
          const scheme = new URL(url).protocol;
          if (scheme !== "https:" && scheme !== "http:") {
            throw failure(-32602, `ui/open-link opens only http(s) links, got ${url}`);
          }
          browser.win.open(url, "_blank", "noopener,noreferrer");
          return {};
        }}
        onFallbackRequest={async (message) => {
          if (message.method !== "ui/update-model-context") {
            throw failure(METHOD_NOT_FOUND, `${message.method} is not supported by the chat`);
          }
          await ask("mcp_app_context", request(message.params || {}));
          return {};
        }}
        onError={(failed) => setError(failed.message)}
      />
    </div>
  );
}
