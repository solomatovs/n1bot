import { useCallback, useEffect, useState } from "react";
import { useRecoilValue } from "recoil";
import { sessionState } from "@chainlit/react-client";
import {
  AlertCircle,
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  KeyRound,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { useTranslation } from "@/components/i18n";

// Приложение MCP-серверов чата: команда /mcp выводит его в ленту сообщением
// с двумя экранами — список серверов и карточка сервера. Состояние экрана
// живёт в компоненте; данные приходят действиями чата (mcp_list, mcp_status,
// mcp_detail, mcp_reconnect), после переподключения сокета список читается
// заново — как подписка на connect в CanvasView.
// Подписи — из переводов chainlit (aid.mcp), тексты ошибок — как пришли.
// Вид — компоненты shadcn и классы tailwind сборки chainlit, своего CSS нет.
const TEXT = "aid.mcp";
const STATES = ["connected", "failed", "needs_auth", "pending", "disabled"];
const DOTS = {
  connected: "bg-green-500",
  failed: "bg-red-500",
  needs_auth: "bg-amber-500",
  disabled: "bg-muted-foreground",
};
const PENDING_DOT = "bg-muted-foreground animate-pulse";
// рамка, фон и значок плашки причины по тону
const NOTES = {
  failure: {
    box: "border-destructive/50 bg-destructive/10",
    icon: "text-red-600 dark:text-red-400",
  },
  warning: {
    box: "border-amber-500/50 bg-amber-500/10",
    icon: "text-amber-600 dark:text-amber-400",
  },
};

function useLabels() {
  const { t } = useTranslation();
  const plain = { interpolation: { escapeValue: false } };
  return {
    title: t(`${TEXT}.title`),
    origin: (origin) => {
      if (origin === "user") return t(`${TEXT}.yours`);
      return t(`${TEXT}.profile`);
    },
    state: (state) => {
      if (STATES.includes(state)) return t(`${TEXT}.state.${state}`);
      return t(`${TEXT}.state.pending`);
    },
    back: t(`${TEXT}.back`),
    reconnect: t(`${TEXT}.reconnect`),
    reconnecting: t(`${TEXT}.reconnecting`),
    transport: t(`${TEXT}.transport`),
    address: t(`${TEXT}.address`),
    tools: t(`${TEXT}.tools`),
    toolCount: (number) => t(`${TEXT}.toolCount`, { ...plain, number }),
    issue: t(`${TEXT}.issue`),
    issueAuth: t(`${TEXT}.issueAuth`),
    showTools: t(`${TEXT}.showTools`),
    hideTools: t(`${TEXT}.hideTools`),
    readOnly: t(`${TEXT}.readOnly`),
    destructive: t(`${TEXT}.destructive`),
    noTools: t(`${TEXT}.noTools`),
    empty: t(`${TEXT}.empty`),
    listFailed: t(`${TEXT}.listFailed`),
    actionFailed: t(`${TEXT}.actionFailed`),
    retry: t(`${TEXT}.retry`),
  };
}

function StateDot({ state }) {
  let color = PENDING_DOT;
  if (DOTS[state]) color = DOTS[state];

  return <span className={`size-2 shrink-0 rounded-full ${color}`} aria-hidden="true" />;
}

function OriginBadge({ origin, labels }) {
  return (
    <Badge variant="outline" className="shrink-0 font-normal text-muted-foreground">
      {labels.origin(origin)}
    </Badge>
  );
}

// Плашка причины: значок и заголовок по тону, ниже текст как пришёл.
function Note({ tone, title, text, children }) {
  const note = NOTES[tone];
  let Icon = AlertCircle;
  if (tone === "warning") Icon = KeyRound;

  return (
    <div
      className={`flex flex-col gap-1.5 rounded-lg border p-3 ${note.box}`}
      role="alert"
      data-mcp-issue
    >
      <div className="flex items-center gap-2 font-medium">
        <Icon className={`size-4 shrink-0 ${note.icon}`} />
        {title}
      </div>
      <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words font-mono text-xs">
        {text}
      </pre>
      {children}
    </div>
  );
}

// Список серверов: mcp_list, затем mcp_status по каждому серверу.
function ServerList({ labels }) {
  const [rows, setRows] = useState(null);
  const [statuses, setStatuses] = useState({});
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);

  const load = useCallback(async () => {
    setError("");
    const answer = await callAction({ name: "mcp_list", payload: {} });
    const response = answer && answer.response;
    if (!response) {
      setError("mcp_list: the chat gave no answer");
      return;
    }
    if (response.error) {
      setError(response.error);
      return;
    }
    setRows(response.servers);
    const next = {};
    await Promise.all(
      response.servers.map(async (row) => {
        const status = await callAction({
          name: "mcp_status",
          payload: { name: row.name },
        });
        const state = status && status.response;
        if (state && !state.error) next[row.name] = state;
      }),
    );
    setStatuses(next);
  }, []);

  useEffect(() => {
    if (selected) return;
    load();
  }, [load, selected]);

  // Сначала серверы, которым нужен вход, — они стоят первыми, как в Claude
  // Code; остальные остаются в порядке профиля, как их отдал mcp_list.
  const first = [];
  const rest = [];
  if (rows) {
    for (const row of rows) {
      const status = statuses[row.name];
      if (status && status.state === "needs_auth") {
        first.push(row);
      } else {
        rest.push(row);
      }
    }
  }
  const sorted = [...first, ...rest];

  if (selected) {
    return <ServerCard name={selected} labels={labels} onBack={() => setSelected(null)} />;
  }

  if (error) {
    return (
      <div className="flex w-full flex-col gap-2 text-sm" data-mcp-servers>
        <div className="font-semibold">{labels.title}</div>
        <Note tone="failure" title={labels.listFailed} text={error}>
          <Button variant="outline" size="sm" className="h-8 w-fit" onClick={load}>
            <RefreshCw />
            {labels.retry}
          </Button>
        </Note>
      </div>
    );
  }

  if (rows === null) {
    return (
      <div className="flex w-full items-center gap-2 text-sm" data-mcp-servers-loading>
        <StateDot state="pending" />
        <span className="font-semibold">{labels.title}</span>
      </div>
    );
  }

  if (!sorted.length) {
    return (
      <div className="flex w-full flex-col gap-2 text-sm" data-mcp-servers-empty>
        <div className="font-semibold">{labels.title}</div>
        <div className="text-muted-foreground">{labels.empty}</div>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-2 text-sm" data-mcp-servers>
      <div className="flex items-baseline gap-2 font-semibold">
        {labels.title}
        <span className="font-normal text-muted-foreground">{sorted.length}</span>
      </div>
      <Card className="divide-y overflow-hidden">
        {sorted.map((row) => (
          <ServerRow
            key={row.name}
            row={row}
            status={statuses[row.name]}
            labels={labels}
            onOpen={() => setSelected(row.name)}
          />
        ))}
      </Card>
    </div>
  );
}

// Строка списка: состояние и число инструментов — одной подписью справа,
// чтобы концы строк стояли ровно при любом наборе полей.
function ServerRow({ row, status, labels, onOpen }) {
  let state = "pending";
  if (status) state = status.state;

  let summary = labels.state(state);
  if (status && status.tools) summary = `${summary} · ${labels.toolCount(status.tools)}`;

  return (
    <button
      type="button"
      className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-accent"
      data-mcp-server={row.name}
      onClick={onOpen}
    >
      <StateDot state={state} />
      <span className="flex min-w-0 items-center gap-2">
        <span className="truncate font-medium">{row.name}</span>
        <OriginBadge origin={row.origin} labels={labels} />
      </span>
      <span className="whitespace-nowrap text-right text-muted-foreground">{summary}</span>
      <ChevronRight className="size-4 text-muted-foreground" aria-hidden="true" />
    </button>
  );
}

// Карточка сервера: адрес, транспорт, состояние, Reconnect и инструменты.
function ServerCard({ name, labels, onBack }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showTools, setShowTools] = useState(false);

  const load = useCallback(async () => {
    const answer = await callAction({ name: "mcp_detail", payload: { name } });
    const response = answer && answer.response;
    if (!response) {
      setError("mcp_detail: the chat gave no answer");
      return;
    }
    if (response.error) {
      setError(response.error);
      return;
    }
    setDetail(response);
    setError("");
  }, [name]);

  useEffect(() => {
    load();
  }, [load]);

  const reconnect = async () => {
    setBusy(true);
    setError("");
    const answer = await callAction({ name: "mcp_reconnect", payload: { name } });
    const response = answer && answer.response;
    setBusy(false);
    if (!response) {
      setError("mcp_reconnect: the chat gave no answer");
      return;
    }
    if (response.error) {
      setError(response.error);
      return;
    }
    await load();
  };

  const back = (
    <Button
      variant="ghost"
      size="sm"
      className="-ml-3 h-8 w-fit text-muted-foreground"
      onClick={onBack}
    >
      <ArrowLeft />
      {labels.back}
    </Button>
  );

  if (!detail) {
    return (
      <div className="flex w-full flex-col gap-2 text-sm" data-mcp-card-loading>
        {back}
        {error ? <Note tone="failure" title={labels.actionFailed} text={error} /> : null}
      </div>
    );
  }

  let issue = null;
  if (detail.issue) {
    let title = labels.issue;
    let tone = "failure";
    if (detail.state === "needs_auth") {
      title = labels.issueAuth;
      tone = "warning";
    }
    issue = <Note tone={tone} title={title} text={detail.issue} />;
  }

  let reconnectLabel = labels.reconnect;
  let spin = "";
  if (busy) {
    reconnectLabel = labels.reconnecting;
    spin = "animate-spin";
  }

  const reconnectable = detail.state === "connected" || detail.state === "failed";
  const connected = detail.state === "connected";
  const tools = detail.tools || [];

  return (
    <div className="flex w-full flex-col gap-2 text-sm" data-mcp-card={detail.row.name}>
      {back}
      <Card>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
          <span className="break-all text-base font-semibold">{detail.row.name}</span>
          <OriginBadge origin={detail.row.origin} labels={labels} />
          <Badge variant="secondary" className="gap-1.5 font-normal">
            <StateDot state={detail.state} />
            {labels.state(detail.state)}
          </Badge>
          {reconnectable ? (
            <Button
              variant="outline"
              size="sm"
              className="ml-auto h-8"
              onClick={reconnect}
              disabled={busy}
            >
              <RefreshCw className={spin} />
              {reconnectLabel}
            </Button>
          ) : null}
        </div>
        <Separator />
        <div className="flex flex-col gap-4 px-4 py-3">
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1.5">
            <dt className="text-muted-foreground">{labels.transport}</dt>
            <dd>{detail.row.transport}</dd>
            <dt className="text-muted-foreground">{labels.address}</dt>
            <dd className="break-all font-mono text-xs leading-5">{detail.row.address}</dd>
            {connected ? (
              <>
                <dt className="text-muted-foreground">{labels.tools}</dt>
                <dd>{tools.length}</dd>
              </>
            ) : null}
          </dl>
          {issue}
          {error ? <Note tone="failure" title={labels.actionFailed} text={error} /> : null}
          {connected ? (
            <ToolList
              tools={tools}
              labels={labels}
              shown={showTools}
              onToggle={() => setShowTools(!showTools)}
            />
          ) : null}
        </div>
      </Card>
    </div>
  );
}

function ToolList({ tools, labels, shown, onToggle }) {
  let Chevron = ChevronRight;
  let label = labels.showTools;
  if (shown) {
    Chevron = ChevronDown;
    label = labels.hideTools;
  }

  let list = null;
  if (shown) {
    list = (
      <div className="max-h-[420px] divide-y overflow-y-auto rounded-md border" data-mcp-tools>
        {tools.length ? (
          tools.map((tool) => <ToolRow key={tool.name} tool={tool} labels={labels} />)
        ) : (
          <div className="px-3 py-2 text-muted-foreground">{labels.noTools}</div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <Button
        variant="ghost"
        size="sm"
        className="-ml-3 h-8 w-fit"
        onClick={onToggle}
        aria-expanded={shown}
      >
        <Chevron />
        {label}
        <span className="font-normal text-muted-foreground">{tools.length}</span>
      </Button>
      {list}
    </div>
  );
}

// Инструмент сервера: заголовок, имя вызова, если отличается, пометки и
// описание в две строки — целиком во всплывающей подсказке.
function ToolRow({ tool, labels }) {
  const title = tool.title || tool.name;
  return (
    <div className="flex flex-col gap-0.5 px-3 py-2" title={tool.description}>
      <div className="flex min-w-0 items-center gap-2">
        <span className="font-medium">{title}</span>
        {title !== tool.name ? (
          <span className="truncate font-mono text-xs text-muted-foreground">{tool.name}</span>
        ) : null}
        <span className="ml-auto flex shrink-0 gap-1.5">
          {tool.read_only ? (
            <Badge variant="outline" className="font-normal text-muted-foreground">
              {labels.readOnly}
            </Badge>
          ) : null}
          {tool.destructive ? (
            <Badge variant="destructive" className="font-normal">
              {labels.destructive}
            </Badge>
          ) : null}
        </span>
      </div>
      {tool.description ? (
        <span className="line-clamp-2 text-muted-foreground">{tool.description}</span>
      ) : null}
    </div>
  );
}

// Приложение после переподключения сокета читает список заново.
export default function McpServers() {
  const labels = useLabels();
  const session = useRecoilValue(sessionState);
  const socket = session && session.socket;
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    if (!socket) return;
    const reload = () => setGeneration((value) => value + 1);
    socket.on("connect", reload);
    return () => socket.off("connect", reload);
  }, [socket]);

  return <ServerList key={generation} labels={labels} />;
}
