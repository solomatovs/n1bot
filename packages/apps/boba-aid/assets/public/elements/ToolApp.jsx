import { useEffect, useMemo, useRef, useState } from "react";
import { AppBridge, AppFrame } from "@mcp-ui/client";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { useTranslation } from "@/components/i18n";
import {
  AppWindow,
  ChevronDown,
  ChevronUp,
  Maximize2,
  Minimize2,
} from "lucide-react";

// Карточка приложения MCP Apps в основной ленте, под контейнером хода. Мост
// AppBridge карточка строит сама и рисует AppFrame: AppRenderer отвечает на
// ui/request-display-mode текущим режимом мимо хоста и шлёт контекст хоста до
// connect(). Запросы приложения уходят действиями chainlit в серверную часть
// чата, та ходит на сервер от имени пользователя вкладки. Контекст хоста — по
// спецификации: тема, переменные стилей из токенов чата и его шрифт с хоста
// песочницы, режим и размеры контейнера. Подписи карточки — из переводов
// chainlit (aid.app), содержимое приложения — как пришло.

// новые props (итог вызова, отмена) карточка принимает на месте: перемонтаж
// перезагрузил бы приложение и потерял его состояние
export const updatesInPlace = true;

const TEXT = "aid.app";
const APP_MIME = "text/html;profile=mcp-app";
const METHOD_NOT_FOUND = -32601;
const INVALID_PARAMS = -32602;
const HOST_INFO = { name: "boba-aid", version: "1" };
const HOST_CAPABILITIES = {
  openLinks: {},
  serverTools: {},
  serverResources: {},
  logging: {},
  message: { text: {} },
  updateModelContext: { text: {}, structuredContent: {} },
};
const INLINE = "inline";
const FULLSCREEN = "fullscreen";
const DISPLAY_MODES = [INLINE, FULLSCREEN];

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
  timeZone() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  },
  matches(query) {
    const win = this.win;
    if (!win) return false;
    return win.matchMedia(query).matches;
  },
  listen(name, handler) {
    const win = this.win;
    if (!win) return;
    win.addEventListener(name, handler);
  },
  unlisten(name, handler) {
    const win = this.win;
    if (!win) return;
    win.removeEventListener(name, handler);
  },
};

// Переменные стилей MCP Apps из токенов темы chainlit — все 76 ключей
// спецификации. Токены — тройки HSL на :root и .dark, theme.json
// переопределяет часть, поэтому значения берутся из вычисленного стиля корня
// при каждой смене темы. Success и warning у chainlit нет — пары tailwind; danger
// на тёмной теме светлее --destructive, который там — заливка под светлым текстом.
const token = (name, alpha) => ({ token: name, alpha });
const fixed = (light, dark) => ({ light, dark });
const value = (text) => ({ light: text, dark: text });
const radius = (shift) => ({ token: "radius", shift });
const hairline = (name) => ({ token: name, hairline: true });

const SUCCESS = fixed("hsl(142 72% 29%)", "hsl(142 69% 58%)");
const WARNING = fixed("hsl(32 95% 44%)", "hsl(43 96% 56%)");
const DANGER = fixed("hsl(0 84.2% 60.2%)", "hsl(0 91% 71%)");
const DANGER_TEXT = fixed("hsl(0 72% 51%)", "hsl(0 91% 71%)");
const DANGER_SOFT = fixed("hsl(0 84.2% 60.2% / 0.12)", "hsl(0 91% 71% / 0.16)");
const SUCCESS_SOFT = fixed("hsl(142 72% 29% / 0.12)", "hsl(142 69% 58% / 0.16)");
const WARNING_SOFT = fixed("hsl(32 95% 44% / 0.12)", "hsl(43 96% 56% / 0.16)");
// токены, которые не тройки HSL, а готовые значения CSS
const PLAIN_TOKENS = ["font-sans", "font-mono", "radius"];

const STYLE_VARIABLES = {
  "--color-background-primary": token("background"),
  "--color-background-secondary": token("secondary"),
  "--color-background-tertiary": token("muted"),
  "--color-background-inverse": token("foreground"),
  "--color-background-ghost": value("transparent"),
  "--color-background-info": token("primary", 0.12),
  "--color-background-danger": DANGER_SOFT,
  "--color-background-success": SUCCESS_SOFT,
  "--color-background-warning": WARNING_SOFT,
  "--color-background-disabled": token("muted", 0.5),
  "--color-text-primary": token("foreground"),
  "--color-text-secondary": token("muted-foreground"),
  "--color-text-tertiary": token("muted-foreground", 0.75),
  "--color-text-inverse": token("background"),
  "--color-text-info": token("primary"),
  "--color-text-danger": DANGER_TEXT,
  "--color-text-success": SUCCESS,
  "--color-text-warning": WARNING,
  "--color-text-disabled": token("muted-foreground", 0.5),
  "--color-text-ghost": token("muted-foreground", 0.4),
  "--color-border-primary": token("border"),
  "--color-border-secondary": token("input"),
  "--color-border-tertiary": token("border", 0.5),
  "--color-border-inverse": token("foreground"),
  "--color-border-ghost": value("transparent"),
  "--color-border-info": token("primary"),
  "--color-border-danger": DANGER,
  "--color-border-success": SUCCESS,
  "--color-border-warning": WARNING,
  "--color-border-disabled": token("border", 0.5),
  "--color-ring-primary": token("ring"),
  "--color-ring-secondary": token("muted-foreground"),
  "--color-ring-inverse": token("background"),
  "--color-ring-info": token("primary"),
  "--color-ring-danger": DANGER,
  "--color-ring-success": SUCCESS,
  "--color-ring-warning": WARNING,
  "--font-sans": token("font-sans"),
  "--font-mono": token("font-mono"),
  "--font-weight-normal": value("400"),
  "--font-weight-medium": value("500"),
  "--font-weight-semibold": value("600"),
  "--font-weight-bold": value("700"),
  "--font-text-xs-size": value("0.75rem"),
  "--font-text-sm-size": value("0.875rem"),
  "--font-text-md-size": value("1rem"),
  "--font-text-lg-size": value("1.125rem"),
  "--font-heading-xs-size": value("0.875rem"),
  "--font-heading-sm-size": value("1rem"),
  "--font-heading-md-size": value("1.125rem"),
  "--font-heading-lg-size": value("1.25rem"),
  "--font-heading-xl-size": value("1.5rem"),
  "--font-heading-2xl-size": value("1.875rem"),
  "--font-heading-3xl-size": value("2.25rem"),
  "--font-text-xs-line-height": value("1rem"),
  "--font-text-sm-line-height": value("1.25rem"),
  "--font-text-md-line-height": value("1.5rem"),
  "--font-text-lg-line-height": value("1.75rem"),
  "--font-heading-xs-line-height": value("1.25rem"),
  "--font-heading-sm-line-height": value("1.5rem"),
  "--font-heading-md-line-height": value("1.75rem"),
  "--font-heading-lg-line-height": value("1.75rem"),
  "--font-heading-xl-line-height": value("2rem"),
  "--font-heading-2xl-line-height": value("2.25rem"),
  "--font-heading-3xl-line-height": value("2.5rem"),
  "--border-radius-xs": radius("- 8px"),
  "--border-radius-sm": radius("- 4px"),
  "--border-radius-md": radius("- 2px"),
  "--border-radius-lg": radius(""),
  "--border-radius-xl": radius("+ 4px"),
  "--border-radius-full": value("9999px"),
  "--border-width-regular": value("1px"),
  "--shadow-hairline": hairline("border"),
  "--shadow-sm": value("0 1px 2px 0 rgb(0 0 0 / 0.05)"),
  "--shadow-md": value("0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)"),
  "--shadow-lg": value("0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)"),
};

// Шрифт чата для приложения: Inter лежит рядом со страницей песочницы
// (fonts/inter/files), её CSP пускает шрифты только со своего источника
const FONT_FILES = "fonts/inter/files/";
const FONT_WEIGHTS = [400, 500, 700];
const FONT_RANGES = {
  latin:
    "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD",
  cyrillic: "U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116",
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

// HTML и CSP ресурса: _meta.ui.csp мост сам не читает; домены, накрывающие
// источник чата, выбрасывает страница-прокси песочницы
function appOf(read) {
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
  return { html, csp: meta.csp };
}

function textOf(content) {
  const parts = [];
  for (const block of content || []) {
    if (block.type === "text") parts.push(block.text);
  }
  return parts.join("\n");
}

function styleValue(style, recipe, dark) {
  if (!recipe.token) {
    if (dark) return recipe.dark;
    return recipe.light;
  }

  const raw = style.getPropertyValue(`--${recipe.token}`).trim();
  if (!raw) {
    throw new Error(`the chat theme has no token --${recipe.token}`);
  }
  if (recipe.shift !== undefined) {
    if (!recipe.shift) return raw;
    return `calc(${raw} ${recipe.shift})`;
  }
  if (PLAIN_TOKENS.includes(recipe.token)) return raw;
  if (recipe.hairline) return `0 0 0 1px hsl(${raw})`;
  if (recipe.alpha === undefined) return `hsl(${raw})`;
  return `hsl(${raw} / ${recipe.alpha})`;
}

function hostStyles(dark, sandboxUrl) {
  const style = getComputedStyle(browser.root());
  const variables = {};
  for (const [name, recipe] of Object.entries(STYLE_VARIABLES)) {
    variables[name] = styleValue(style, recipe, dark);
  }

  const faces = [];
  for (const weight of FONT_WEIGHTS) {
    for (const [subset, range] of Object.entries(FONT_RANGES)) {
      const file = `${FONT_FILES}inter-${subset}-${weight}-normal.woff2`;
      const url = new URL(file, sandboxUrl).href;
      faces.push(
        `@font-face{font-family:'Inter';font-style:normal;font-display:swap;` +
          `font-weight:${weight};src:url(${url}) format('woff2');unicode-range:${range}}`
      );
    }
  }
  return { variables, css: { fonts: faces.join("\n") } };
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

// внутренний размер тела карточки: ширина места приложения, на весь экран — и
// высота; mode — режим, в котором размер измерен: после смены режима прежний
// размер устарел, пока тело не перемерено
function useBox(ref, modeRef) {
  const [size, setSize] = useState({ width: 0, height: 0, mode: INLINE });
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const observer = new ResizeObserver(() => {
      const style = getComputedStyle(node);
      const across = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      const down = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      const width = Math.round(node.clientWidth - across);
      const height = Math.round(node.clientHeight - down);
      const mode = modeRef.current;
      if (width <= 0) return;
      setSize((was) => {
        if (was.width === width && was.height === height && was.mode === mode) return was;
        return { width, height, mode };
      });
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return size;
}

// значение props, которое меняется только с содержимым: мост шлёт tool-input и
// tool-result на каждую смену объекта, а props приходят новым объектом
function useSettled(raw) {
  const key = JSON.stringify(raw === undefined ? null : raw);
  return useMemo(() => JSON.parse(key), [key]);
}

function HeadButton({ action, title, onClick, children }) {
  return (
    <Button
      variant="ghost"
      size="icon"
      className="text-muted-foreground"
      onClick={onClick}
      title={title}
      aria-label={title}
      data-app-action={action}
    >
      {children}
    </Button>
  );
}

export default function ToolApp() {
  const { t } = useTranslation();
  const isolated = originOf(props.sandbox_url) !== browser.origin();
  const dark = useDark();
  const bodyRef = useRef(null);
  const [mode, setMode] = useState(INLINE);
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const size = useBox(bodyRef, modeRef);
  const [app, setApp] = useState(null);
  const [error, setError] = useState(null);
  const [modes, setModes] = useState(DISPLAY_MODES);
  const [collapsed, setCollapsed] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [sized, setSized] = useState(false);
  const input = useSettled(props.input);
  const result = useSettled(props.result);
  const cancelled = props.cancelled;
  const name = props.tool.name;

  const request = (params) => ({
    server: props.server,
    call_id: props.call_id,
    params,
  });

  useEffect(() => {
    if (!isolated) return undefined;
    let dropped = false;
    ask("mcp_app_read", request({ uri: props.resource_uri }))
      .then((read) => {
        if (!dropped) setApp(appOf(read));
      })
      .catch((failed) => {
        if (!dropped) setError(failed.message);
      });
    return () => {
      dropped = true;
    };
  }, [props.server, props.resource_uri, isolated]);

  useEffect(() => {
    if (mode !== FULLSCREEN) return undefined;
    const pressed = (event) => {
      if (event.key === "Escape") setMode(INLINE);
    };
    browser.listen("keydown", pressed);
    return () => browser.unlisten("keydown", pressed);
  }, [mode]);

  const sandbox = useMemo(() => {
    if (!app) return null;
    return { url: new URL(props.sandbox_url), csp: app.csp };
  }, [props.sandbox_url, app]);

  const styles = useMemo(() => hostStyles(dark, props.sandbox_url), [dark, props.sandbox_url]);

  // высота тела входит в контекст только на весь экран: в ленте её задаёт
  // само приложение (size-changed), и новый контекст на каждую смену высоты
  // гонял бы приложение по кругу host-context-changed → size-changed
  let height = null;
  if (mode === FULLSCREEN) height = size.height;
  const hostContext = useMemo(() => {
    let containerDimensions = { width: size.width };
    if (height !== null) {
      containerDimensions = { width: size.width, height };
    }
    return {
      theme: dark ? "dark" : "light",
      styles,
      displayMode: mode,
      availableDisplayModes: DISPLAY_MODES,
      containerDimensions,
      locale: browser.language(),
      timeZone: browser.timeZone(),
      userAgent: `${HOST_INFO.name}/${HOST_INFO.version}`,
      platform: "web",
      deviceCapabilities: {
        touch: browser.matches("(pointer: coarse)"),
        hover: browser.matches("(hover: hover)"),
      },
      toolInfo: { id: props.call_id, tool: props.tool },
    };
  }, [dark, styles, mode, size.width, height]);

  const latest = useRef(hostContext);
  latest.current = hostContext;
  const allowed = useRef(DISPLAY_MODES);

  // мост — один на карточку; стартовый контекст уходит ответом на ui/initialize
  const bridge = useMemo(() => {
    if (!app || !size.width) return null;
    const made = new AppBridge(null, HOST_INFO, HOST_CAPABILITIES, {
      hostContext: latest.current,
    });
    made.oncalltool = (params) => ask("mcp_app_call", request(params));
    made.onreadresource = (params) => ask("mcp_app_read", request(params));
    made.onmessage = async (params) => {
      const text = textOf(params.content);
      if (!text) throw failure(INVALID_PARAMS, "ui/message carries no text");
      await sendUserMessage(text);
      return {};
    };
    made.onopenlink = async ({ url }) => {
      const scheme = new URL(url).protocol;
      if (scheme !== "https:" && scheme !== "http:") {
        throw failure(INVALID_PARAMS, `ui/open-link opens only http(s) links, got ${url}`);
      }
      browser.win.open(url, "_blank", "noopener,noreferrer");
      return {};
    };
    made.onupdatemodelcontext = async (params) => {
      await ask("mcp_app_context", request(params));
      return {};
    };
    made.onrequestdisplaymode = async ({ mode: wanted }) => {
      if (!allowed.current.includes(wanted)) {
        return { mode: latest.current.displayMode };
      }
      setCollapsed(false);
      setMode(wanted);
      return { mode: wanted };
    };
    made.onloggingmessage = (params) => {
      console.debug(`mcp app ${props.server}/${name}: ${params.level}:`, params.data);
    };
    made.fallbackRequestHandler = async (asked) => {
      throw failure(METHOD_NOT_FOUND, `${asked.method} is not supported by the chat`);
    };
    return made;
  }, [app, size.width > 0]);

  // смена режима уходит приложению вместе с размерами тела в новом режиме
  useEffect(() => {
    if (!bridge || !initialized) return;
    if (size.mode !== mode) return;
    bridge.setHostContext(hostContext);
  }, [bridge, initialized, hostContext]);

  useEffect(() => {
    if (!bridge || !initialized || !cancelled) return;
    bridge.sendToolCancelled({ reason: cancelled });
  }, [bridge, initialized, cancelled]);

  useEffect(() => {
    if (!bridge) return undefined;
    return () => {
      bridge
        .teardownResource({})
        .catch((failed) =>
          console.debug(`ui/resource-teardown of the app of call ${props.call_id}: ${failed.message}`)
        )
        .finally(() => bridge.close());
    };
  }, [bridge]);

  const started = (info) => {
    const declared = info.appCapabilities && info.appCapabilities.availableDisplayModes;
    let shown = DISPLAY_MODES;
    if (declared) shown = DISPLAY_MODES.filter((each) => declared.includes(each));
    allowed.current = shown;
    setModes(shown);
    setInitialized(true);
  };

  let failed = error;
  if (!isolated) {
    failed = t(`${TEXT}.sameOrigin`, {
      url: props.sandbox_url,
      interpolation: { escapeValue: false },
    });
  }

  let body = null;
  if (failed) {
    body = (
      <div
        className="px-2 pt-1 pb-2 text-sm text-red-600 dark:text-red-400"
        data-mcp-app-error=""
      >
        {failed}
      </div>
    );
  } else if (bridge) {
    body = (
      <AppFrame
        html={app.html}
        sandbox={sandbox}
        appBridge={bridge}
        toolInput={input}
        toolResult={result || undefined}
        onInitialized={started}
        onSizeChanged={() => setSized(true)}
        onError={(broken) => setError(broken.message)}
      />
    );
  }

  let waiting = null;
  if (!failed && !initialized && !sized) {
    waiting = (
      <div
        className="absolute inset-x-2 top-1 bottom-2 z-[1] flex items-center justify-center rounded-sm bg-accent text-sm text-muted-foreground"
        data-mcp-app-loading=""
      >
        {t(`${TEXT}.loading`)}
      </div>
    );
  }

  const full = mode === FULLSCREEN;
  let frame = "relative overflow-hidden";
  let place = "relative px-2 pt-1 pb-2 [&_iframe]:block [&_iframe]:!w-full [&_iframe]:border-0 [&_iframe]:rounded-sm";
  if (full) {
    frame = "fixed inset-0 z-50 flex flex-col overflow-hidden rounded-none border-0 shadow-none";
    place = `${place} flex-1 min-h-0 [&>div]:h-full [&_iframe]:!h-full`;
  }
  if (waiting) place = `${place} min-h-[128px]`;
  if (collapsed && !full) place = `${place} hidden`;

  let expand = null;
  if (modes.includes(FULLSCREEN) && !full) {
    expand = (
      <HeadButton
        action="fullscreen"
        title={t(`${TEXT}.fullscreen`)}
        onClick={() => {
          setCollapsed(false);
          setMode(FULLSCREEN);
        }}
      >
        <Maximize2 />
      </HeadButton>
    );
  }

  let trail = (
    <HeadButton
      action="collapse"
      title={t(`${TEXT}.collapse`)}
      onClick={() => setCollapsed(true)}
    >
      <ChevronUp />
    </HeadButton>
  );
  if (collapsed) {
    trail = (
      <HeadButton
        action="expand"
        title={t(`${TEXT}.expand`)}
        onClick={() => setCollapsed(false)}
      >
        <ChevronDown />
      </HeadButton>
    );
  }
  if (full) {
    trail = (
      <HeadButton action="exit" title={t(`${TEXT}.exit`)} onClick={() => setMode(INLINE)}>
        <Minimize2 />
      </HeadButton>
    );
  }

  return (
    <Card
      className={frame}
      data-mcp-app={props.call_id}
      data-display-mode={mode}
      data-collapsed={collapsed ? "true" : "false"}
    >
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0 py-1 px-4 min-h-[44px]">
        <span className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
          <AppWindow className="h-4 w-4 shrink-0" />
          <span className="truncate" data-mcp-app-server="">
            {props.server}
          </span>
          <span className="opacity-70">·</span>
          <span className="truncate font-medium text-foreground" data-mcp-app-tool="">
            {name}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1">
          {expand}
          {trail}
        </span>
      </CardHeader>
      <div ref={bodyRef} className={place}>
        {waiting}
        {body}
      </div>
    </Card>
  );
}
