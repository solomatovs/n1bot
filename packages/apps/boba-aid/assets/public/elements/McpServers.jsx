import { useCallback, useEffect, useState } from "react";
import { useRecoilValue } from "recoil";
import { sessionState } from "@chainlit/react-client";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Plug,
  Plus,
  Search,
  Server,
  Settings2,
  Trash2,
  Pencil,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { useTranslation } from "@/components/i18n";

// Приложение MCP-серверов чата: команда /mcp выводит его в ленту сообщением
// со списком серверов, карточкой и диалогом правки. Состояние экрана
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
    add: t(`${TEXT}.add`),
    create: t(`${TEXT}.create`),
    search: t(`${TEXT}.search`),
    noMatches: t(`${TEXT}.noMatches`),
    edit: t(`${TEXT}.edit`),
    save: t(`${TEXT}.save`),
    saving: t(`${TEXT}.saving`),
    cancel: t(`${TEXT}.cancel`),
    remove: t(`${TEXT}.remove`),
    removeTitle: t(`${TEXT}.removeTitle`),
    removeHelp: t(`${TEXT}.removeHelp`),
    confirmName: t(`${TEXT}.confirmName`),
    name: t(`${TEXT}.name`),
    nameHint: t(`${TEXT}.nameHint`),
    auth: t(`${TEXT}.auth`),
    authNone: t(`${TEXT}.authNone`),
    authBearer: t(`${TEXT}.authBearer`),
    authOAuth: t(`${TEXT}.authOAuth`),
    oauthHelp: t(`${TEXT}.oauthHelp`),
    oauthConnect: t(`${TEXT}.oauthConnect`),
    oauthDisconnect: t(`${TEXT}.oauthDisconnect`),
    authLocal: t(`${TEXT}.authLocal`),
    authProxy: t(`${TEXT}.authProxy`),
    token: t(`${TEXT}.token`),
    tokenSaved: t(`${TEXT}.tokenSaved`),
    headers: t(`${TEXT}.headers`),
    headerName: t(`${TEXT}.headerName`),
    headerValue: t(`${TEXT}.headerValue`),
    headerAdd: t(`${TEXT}.headerAdd`),
    headerRemove: t(`${TEXT}.headerRemove`),
    enabled: t(`${TEXT}.enabled`),
    disable: t(`${TEXT}.disable`),
    enable: t(`${TEXT}.enable`),
    limits: t(`${TEXT}.limits`),
    formTitle: t(`${TEXT}.formTitle`),
    formHelp: t(`${TEXT}.formHelp`),
    editTitle: t(`${TEXT}.editTitle`),
    advanced: t(`${TEXT}.advanced`),
    urlHint: t(`${TEXT}.urlHint`),
    urlLabel: t(`${TEXT}.urlLabel`),
    settings: t(`${TEXT}.settings`),
    roleManaged: t(`${TEXT}.roleManaged`),

    origin: (origin) => {
      if (origin === "user") return t(`${TEXT}.yours`);
      return t(`${TEXT}.role`);
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

function StateDot({ state, large = false }) {
  let color = PENDING_DOT;
  if (DOTS[state]) color = DOTS[state];

  return <span className={`inline-block ${large ? "size-3" : "size-2"} shrink-0 rounded-full ${color}`} aria-hidden="true" />;
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
      <p className="whitespace-pre-wrap break-words text-sm">
        {text}
      </p>
      {children}
    </div>
  );
}

async function ask(name, payload) {
  const answer = await callAction({ name, payload });
  const response = answer && answer.response;
  if (!response) throw new Error(`${name}: the chat gave no answer`);
  if (response.error) throw new Error(response.error);
  return response;
}

function ListHeading({ labels, count, query, onQuery, onAdd }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 text-base font-semibold">
        <Plug className="size-4 text-muted-foreground" aria-hidden="true" />
        <span>{labels.title}</span>
        {count !== undefined ? <Badge variant="secondary" className="font-normal">{count}</Badge> : null}
      </div>
      <div className="flex items-center gap-2">
        <label className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <span className="sr-only">{labels.search}</span>
          <input
            className="h-10 w-full rounded-md border border-input bg-background pl-9 pr-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            type="search"
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            placeholder={labels.search}
            data-mcp-search
          />
        </label>
        <Button variant="outline" size="icon" className="size-10 shrink-0" onClick={onAdd} aria-label={labels.add} title={labels.add} data-mcp-add>
          <Plus className="size-5" />
        </Button>
      </div>
    </div>
  );
}

// Список серверов: mcp_list, затем mcp_status по каждому серверу.
function ServerList({ labels }) {
  const [rows, setRows] = useState(null);
  const [statuses, setStatuses] = useState({});
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [editing, setEditing] = useState(null);
  const [query, setQuery] = useState("");
  const [busyName, setBusyName] = useState("");
  const [actionError, setActionError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const response = await ask("mcp_list", {});
      setRows(response.servers);
      setStatuses({});
      await Promise.all(response.servers.map(async (row) => {
        let status;
        try {
          status = await ask("mcp_status", { name: row.name });
        } catch (failed) {
          status = { name: row.name, state: "failed", issue: failed.message };
        }
        setStatuses((was) => ({ ...was, [row.name]: status }));
      }));
    } catch (failed) {
      setError(failed.message);
    }
  }, []);

  useEffect(() => {
    if (selected || editing) return;
    load();
  }, [load, selected, editing]);

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
  const visible = sorted.filter((row) => row.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const reconnect = async (name) => {
    setBusyName(name);
    setActionError("");
    try {
      await ask("mcp_reconnect", { name });
      const status = await ask("mcp_status", { name });
      setStatuses((was) => ({ ...was, [name]: status }));
    } catch (failed) {
      setActionError(failed.message);
    } finally {
      setBusyName("");
    }
  };

  const heading = <ListHeading labels={labels} count={rows ? rows.length : undefined}
    query={query} onQuery={setQuery} onAdd={() => setEditing({ current: null, removing: false })} />;

  let content;
  if (selected) {
    content = <ServerCard name={selected} labels={labels} onBack={() => setSelected(null)}
      onEdit={() => setEditing({ current: selected, removing: false })} />;
  } else if (error) {
    content = <Note tone="failure" title={labels.listFailed} text={error}>
      <Button variant="outline" size="sm" className="h-8 w-fit" onClick={load}>
        <RefreshCw />{labels.retry}
      </Button>
    </Note>;
  } else if (rows === null) {
    content = <div className="flex items-center gap-2 text-muted-foreground" data-mcp-servers-loading>
      <StateDot state="pending" />{labels.state("pending")}
    </div>;
  } else if (!visible.length) {
    content = <div className="rounded-lg border border-dashed px-4 py-6 text-center text-muted-foreground" data-mcp-servers-empty>
      {sorted.length ? labels.noMatches : labels.empty}
    </div>;
  } else {
    content = <div className="flex flex-col gap-2">
      {visible.map((row) => (
        <ServerRow
          key={row.name}
          row={row}
          status={statuses[row.name]}
          labels={labels}
          busy={busyName === row.name}
          onOpen={() => setSelected(row.name)}
          onReconnect={() => reconnect(row.name)}
          onEdit={() => setEditing({ current: row.name, removing: false })}
          onRemove={() => setEditing({ current: row.name, removing: true })}
        />
      ))}
    </div>;
  }

  return (
    <>
      {selected ? content : <div className="flex w-full flex-col gap-3 text-sm" data-mcp-servers>
        {heading}
        {actionError ? <Note tone="failure" title={labels.actionFailed} text={actionError} /> : null}
        {content}
      </div>}
      {editing ? <ServerForm current={editing.current} startRemoving={editing.removing} labels={labels}
        onBack={() => setEditing(null)}
        onSaved={(name) => { setSelected(name); setEditing(null); }}
        onRemoved={() => { setSelected(null); setEditing(null); }} /> : null}
    </>
  );
}

// Строка списка: открытие карточки и действия — отдельные кнопки без вложенности.
function ServerRow({ row, status, labels, busy, onOpen, onReconnect, onEdit, onRemove }) {
  let state = "pending";
  if (status) state = status.state;

  let summary = labels.state(state);
  if (status && status.tools) summary = `${summary} · ${labels.toolCount(status.tools)}`;

  return <Card className="flex min-w-0 items-center gap-1 overflow-hidden px-2 py-2" data-mcp-server-card={row.name}>
    <button type="button" className="flex min-w-0 flex-1 items-center gap-3 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      data-mcp-server={row.name} onClick={onOpen}>
      <span className="relative flex size-10 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground" aria-hidden="true">
        <Server className="size-5" />
        <span className="absolute -bottom-1 -right-1 inline-flex rounded-full border-2 border-card bg-card"><StateDot state={state} large /></span>
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex min-w-0 items-center gap-2"><span className="truncate font-semibold">{row.name}</span><OriginBadge origin={row.origin} labels={labels} /></span>
        <span className="truncate text-xs text-muted-foreground">{summary}</span>
      </span>
    </button>
    <div className="flex shrink-0 items-center gap-0.5">
      {row.enabled ? <Button variant="ghost" size="icon" className="size-8 text-muted-foreground" onClick={onReconnect} disabled={busy}
        aria-label={`${labels.reconnect}: ${row.name}`} title={labels.reconnect} data-mcp-row-reconnect={row.name}>
        <RefreshCw className={`size-4 ${busy ? "animate-spin" : ""}`} />
      </Button> : null}
      <Button variant="ghost" size="icon" className="size-8 text-muted-foreground" onClick={onEdit}
        aria-label={`${row.editable ? labels.edit : labels.settings}: ${row.name}`} title={row.editable ? labels.edit : labels.settings} data-mcp-row-edit={row.name}>
        {row.editable ? <Pencil className="size-4" /> : <Settings2 className="size-4" />}
      </Button>
      {row.editable ? <Button variant="ghost" size="icon" className="size-8 text-destructive hover:text-destructive" onClick={onRemove}
        aria-label={`${labels.remove}: ${row.name}`} title={labels.remove} data-mcp-row-remove={row.name}>
        <Trash2 className="size-4" />
      </Button> : null}
    </div>
  </Card>;
}

// Карточка сервера: адрес, транспорт, состояние, Reconnect и инструменты.
function ServerCard({ name, labels, onBack, onEdit }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showTools, setShowTools] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await ask("mcp_detail", { name });
      setDetail(response);
      setError("");
    } catch (failed) {
      setError(failed.message);
    }
  }, [name]);

  useEffect(() => { load(); }, [load]);

  const change = async (action, payload) => {
    setBusy(true);
    setError("");
    try {
      await ask(action, payload);
      await load();
    } catch (failed) {
      setError(failed.message);
    } finally {
      setBusy(false);
    }
  };

  const reconnect = () => change("mcp_reconnect", { name });
  const disconnectOAuth = () => change("mcp_oauth_disconnect", { name });
  const connectOAuth = async () => {
    const returnUrl = window.location.href;
    setBusy(true);
    setError("");
    try {
      const response = await ask("mcp_oauth_start", { name, return_url: returnUrl });
      if (response.url) {
        window.location.assign(response.url);
      } else {
        await load();
        setBusy(false);
      }
    } catch (failed) {
      setError(failed.message);
      setBusy(false);
    }
  };
  const toggle = () => change("mcp_toggle", { name, enabled: !detail.row.enabled });

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

  const reconnectable = detail.row.enabled;
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
          <Button variant="outline" size="sm" className={`ml-auto h-8 ${detail.row.editable ? "" : "text-muted-foreground"}`} onClick={onEdit} disabled={busy}
            data-mcp-edit={detail.row.editable || undefined} data-mcp-settings={!detail.row.editable || undefined}>
            <Pencil />{detail.row.editable ? labels.edit : labels.settings}
          </Button>
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
          <dl className="grid grid-cols-1 gap-y-1.5 sm:grid-cols-[max-content_minmax(0,1fr)] sm:gap-x-6">
            <dt className="text-muted-foreground">{labels.transport}</dt>
            <dd>{detail.row.transport}</dd>
            <dt className="text-muted-foreground">{labels.address}</dt>
            <dd className="break-all font-mono text-xs leading-5">{detail.row.address}</dd>
            <dt className="text-muted-foreground">{labels.auth}</dt>
            <dd className="flex flex-col items-start gap-2">
              <span>{({ none: labels.authNone, bearer: labels.authBearer, oauth: labels.authOAuth, local: labels.authLocal, proxy: labels.authProxy })[detail.row.auth]}</span>
              <div className="flex flex-wrap gap-2">
          {detail.row.auth === "oauth" && detail.row.enabled && detail.state !== "connected" ? (
            <Button variant="default" size="sm" className="h-8" onClick={connectOAuth} disabled={busy} data-mcp-oauth-connect>
              <KeyRound />{labels.oauthConnect}
            </Button>
          ) : null}
          {detail.row.auth === "oauth" && detail.state === "connected" ? (
            <Button variant="outline" size="sm" className="h-8" onClick={disconnectOAuth} disabled={busy} data-mcp-oauth-disconnect>
              <KeyRound />{labels.oauthDisconnect}
            </Button>
          ) : null}
              </div>
            </dd>
            <dt className="text-muted-foreground">{labels.limits}</dt>
            <dd>{detail.row.connect_timeout_sec} s / {detail.row.call_timeout_sec} s</dd>
            <dt className="text-muted-foreground">{labels.headers}</dt>
            <dd className="break-all">{(detail.row.headers || []).join(", ") || "—"}</dd>
            {connected ? (
              <>
                <dt className="text-muted-foreground">{labels.tools}</dt>
                <dd>{tools.length}</dd>
              </>
            ) : null}
          </dl>
          {detail.row.editable ? (
            <Button variant="outline" size="sm" className="h-8 w-fit" onClick={toggle} disabled={busy} data-mcp-toggle>
              {detail.row.enabled ? labels.disable : labels.enable}
            </Button>
          ) : null}
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

function ChoiceGroup({ label, value, options, onChange, disabled, kind }) {
  return <div className="flex flex-col gap-2">
    <span className="font-medium">{label}</span>
    <div role="radiogroup" aria-label={label} className="flex flex-wrap gap-2" data-mcp-choice={kind}>
      {options.map((option) => <button key={option.value} type="button" role="radio"
        name={`mcp-${kind}`} value={option.value}
        className={`inline-flex min-h-10 items-center gap-2 rounded-md border px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed ${value === option.value ? "border-primary bg-primary/10 text-foreground" : "border-input bg-background text-muted-foreground hover:bg-accent"}`}
        aria-checked={value === option.value} onClick={() => onChange(option.value)} disabled={disabled}>
        <span className="inline-flex size-4 shrink-0 items-center justify-center rounded-full border border-current" aria-hidden="true">
          {value === option.value ? <Check className="size-3" /> : null}
        </span>
        {option.label}
      </button>)}
    </div>
  </div>;
}

function ServerForm({ current, startRemoving, labels, onBack, onSaved, onRemoved }) {
  const [form, setForm] = useState({ name: "", transport: "streamable-http", url: "", auth: "none", token: "", headers: [] });
  const [ready, setReady] = useState(!current);
  const [editable, setEditable] = useState(!current);
  const [hasToken, setHasToken] = useState(false);
  const [savedName, setSavedName] = useState(current);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(startRemoving);
  const [confirmation, setConfirmation] = useState("");
  const inputClass = "h-10 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground";

  useEffect(() => {
    if (!current) return;
    let cancelled = false;
    const load = async () => {
      try {
        const view = await ask("mcp_form", { name: current });
        if (cancelled) return;
        setForm({ name: view.name, transport: view.transport, url: view.url, auth: view.auth,
          token: "", headers: view.headers.map((name) => ({ name, value: "", saved: true })) });
        setHasToken(view.has_token);
        setEditable(view.editable === true);
        setReady(true);
      } catch (failed) {
        if (!cancelled) setError(failed.message);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [current]);

  const field = (key, value) => setForm((was) => ({ ...was, [key]: value }));
  const header = (at, key, value) => field("headers", form.headers.map((row, index) => {
    if (index !== at) return row;
    return { ...row, [key]: value };
  }));

  const persist = async () => {
    const clean = { ...form, token: form.auth === "bearer" ? form.token : "",
      headers: form.headers.map(({ name, value }) => ({ name, value })) };
    const action = savedName ? "mcp_update" : "mcp_add";
    const payload = savedName ? { current: savedName, form: clean } : { form: clean };
    const detail = await ask(action, payload);
    setSavedName(detail.row.name);
    return detail.row.name;
  };

  const save = async (event) => {
    event.preventDefault();
    if (!editable) return;
    setBusy(true);
    setError("");
    try {
      onSaved(await persist());
    } catch (failed) {
      setError(failed.message);
    } finally {
      setBusy(false);
    }
  };

  const connectOAuth = async (event) => {
    if (!event.currentTarget.form.reportValidity()) return;
    const returnUrl = window.location.href;
    setBusy(true);
    setError("");
    try {
      let name = savedName;
      if (editable) {
        name = await persist();
        await ask("mcp_toggle", { name, enabled: true });
      }
      const response = await ask("mcp_oauth_start", { name, return_url: returnUrl });
      if (response.url) {
        window.location.assign(response.url);
      } else {
        onSaved(name);
      }
    } catch (failed) {
      setError(failed.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!ready || !editable) return;
    setBusy(true);
    setError("");
    try {
      await ask("mcp_remove", { name: savedName });
      onRemoved();
    } catch (failed) {
      setError(failed.message);
    } finally {
      setBusy(false);
    }
  };

  const transportOptions = editable ? [
    { value: "streamable-http", label: "Streamable HTTP" },
    { value: "sse", label: "SSE" },
  ] : [{ value: form.transport, label: form.transport === "streamable-http" ? "Streamable HTTP" : form.transport.toUpperCase() }];
  const authOptions = editable ? [
    { value: "none", label: labels.authNone },
    { value: "bearer", label: labels.authBearer },
    { value: "oauth", label: labels.authOAuth },
  ] : [{ value: form.auth, label: ({ none: labels.authNone, bearer: labels.authBearer, oauth: labels.authOAuth, local: labels.authLocal, proxy: labels.authProxy })[form.auth] || form.auth }];

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onBack(); }}>
      <DialogContent className="max-h-[90vh] w-[calc(100%-2rem)] max-w-2xl overflow-y-auto p-0" data-mcp-form data-mcp-readonly={ready && !editable || undefined}>
        {removing ? <div className="flex flex-col gap-5 p-6" data-mcp-remove-confirm>
          <DialogHeader>
            <DialogTitle>{labels.removeTitle}</DialogTitle>
            <DialogDescription>{labels.removeHelp} <strong>{savedName}</strong></DialogDescription>
          </DialogHeader>
          {error ? <Note tone="failure" title={labels.actionFailed} text={error} /> : null}
          <input className={inputClass} aria-label={labels.confirmName} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={busy || !ready} autoComplete="off" />
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="destructive" disabled={busy || !ready || !editable || confirmation !== savedName} onClick={remove}><Trash2 />{labels.remove}</Button>
            <Button variant="outline" disabled={busy} onClick={() => setRemoving(false)}>{labels.cancel}</Button>
          </div>
        </div> : <div className="flex flex-col gap-4 p-5 sm:gap-5 sm:p-6">
          <Button variant="ghost" size="sm" className="-ml-3 h-8 w-fit text-muted-foreground" onClick={onBack} disabled={busy}>
            <ArrowLeft />{labels.back}
          </Button>
          <DialogHeader>
            <div className="flex items-center gap-3">
              <span className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-muted text-muted-foreground"><Server className="size-5" /></span>
              <div className="text-left"><DialogTitle>{savedName ? (editable ? labels.editTitle : labels.settings) : labels.formTitle}</DialogTitle>
                <DialogDescription className="mt-1">{labels.formHelp}</DialogDescription></div>
            </div>
          </DialogHeader>
          {ready && !editable ? <p className="rounded-lg border bg-muted/70 p-3 text-sm text-muted-foreground" data-mcp-role-managed>{labels.roleManaged}</p> : null}
          {error ? <Note tone="failure" title={labels.actionFailed} text={error} /> : null}
          <form onSubmit={save} className="flex flex-col gap-4 sm:gap-5">
            <fieldset disabled={busy || !ready || !editable} className={`flex min-w-0 flex-col gap-4 sm:gap-5 ${ready && !editable ? "text-muted-foreground" : ""}`}>
              <label className="flex flex-col gap-2 font-medium">
                {labels.name} <input className={inputClass} name="mcp-name" value={form.name} onChange={(event) => field("name", event.target.value)} maxLength={64} required />
                <span className="text-xs font-normal text-muted-foreground">{labels.nameHint}</span>
              </label>
              <label className="flex flex-col gap-2 font-medium">
                {labels.urlLabel} <input className={inputClass} name="mcp-url" type="url" value={form.url} onChange={(event) => field("url", event.target.value)} required autoComplete="off" spellCheck={false} placeholder="https://mcp.example.com/mcp" />
                <span className="text-xs font-normal text-muted-foreground">{labels.urlHint}</span>
              </label>
              <ChoiceGroup kind="transport" label={labels.transport} value={form.transport} options={transportOptions}
                onChange={(value) => field("transport", value)} disabled={busy || !ready || !editable} />
              <ChoiceGroup kind="auth" label={labels.auth} value={form.auth} options={authOptions}
                onChange={(value) => field("auth", value)} disabled={busy || !ready || !editable} />
              {form.auth === "bearer" ? <label className="flex flex-col gap-2 font-medium">
                {labels.token}
                <input className={inputClass} name="mcp-token" type="password" autoComplete="new-password" value={form.token}
                  onChange={(event) => field("token", event.target.value)} placeholder={hasToken ? labels.tokenSaved : ""} />
              </label> : null}
            </fieldset>
            {form.auth === "oauth" ? <section className="flex flex-col items-start gap-3 rounded-lg border p-4" data-mcp-oauth-form>
              <p className="text-sm text-muted-foreground">{labels.oauthHelp}</p>
              <Button type="button" onClick={connectOAuth} disabled={busy || !ready} data-mcp-oauth-connect>
                <KeyRound />{labels.oauthConnect}
              </Button>
            </section> : null}
            <fieldset disabled={busy || !ready || !editable} className="flex min-w-0 flex-col gap-4 sm:gap-5">
              <details className="rounded-lg border p-3" data-mcp-advanced>
                <summary className="cursor-pointer font-medium">{labels.advanced} · {labels.headers} {form.headers.length ? `(${form.headers.length})` : ""}</summary>
                <div className="mt-4 flex flex-col gap-3">
                  {form.headers.map((row, index) => <div key={index} className="flex flex-wrap items-center gap-2" data-mcp-header>
                    <input className={`${inputClass} min-w-32 flex-1`} aria-label={labels.headerName} value={row.name} onChange={(event) => header(index, "name", event.target.value)} maxLength={64} required />
                    <input className={`${inputClass} min-w-32 flex-1`} type="password" aria-label={labels.headerValue} autoComplete="new-password" value={row.value}
                      onChange={(event) => header(index, "value", event.target.value)} placeholder={row.saved ? labels.tokenSaved : ""} />
                    <Button type="button" variant="ghost" size="icon" onClick={() => field("headers", form.headers.filter((_, at) => at !== index))} aria-label={labels.headerRemove} title={labels.headerRemove}>
                      <Trash2 className="size-4" />
                    </Button>
                  </div>)}
                  <Button type="button" variant="outline" size="sm" className="w-fit" onClick={() => field("headers", [...form.headers, { name: "", value: "" }])}>
                    <Plus className="size-4" />{labels.headerAdd}
                  </Button>
                </div>
              </details>
              {editable ? <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button type="button" variant="outline" onClick={onBack}>{labels.cancel}</Button>
                <Button type="submit" disabled={busy}>{busy ? labels.saving : savedName ? labels.save : labels.create}</Button>
              </div> : null}
            </fieldset>
          </form>
          {savedName && ready && editable ? <div className="border-t pt-4">
            <Button variant="ghost" className="text-destructive hover:text-destructive" disabled={busy} onClick={() => setRemoving(true)} data-mcp-remove><Trash2 />{labels.remove}</Button>
          </div> : null}
        </div>}
      </DialogContent>
    </Dialog>
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

function ToolRow({ tool, labels }) {
  return (
    <details className="min-w-0 p-3" data-mcp-tool={tool.name}>
      <summary className="cursor-pointer">
        <span className="ml-1 break-all font-mono font-medium" data-mcp-tool-name>{tool.name}</span>
        <span className="ml-2 inline-flex gap-1.5 align-middle">
          {tool.annotations?.readOnlyHint ? (
            <Badge variant="outline" className="font-normal text-muted-foreground">{labels.readOnly}</Badge>
          ) : null}
          {tool.annotations?.destructiveHint ? (
            <Badge variant="destructive" className="font-normal">{labels.destructive}</Badge>
          ) : null}
        </span>
        {tool.description ? (
          <span className="mt-1 block line-clamp-2 break-words text-sm text-muted-foreground" data-mcp-tool-description>{tool.description}</span>
        ) : null}
      </summary>
      <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 text-xs leading-5" data-mcp-tool-json>
        <code>{JSON.stringify(tool, null, 2)}</code>
      </pre>
    </details>
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
