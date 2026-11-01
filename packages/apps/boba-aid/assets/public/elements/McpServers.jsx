import { useCallback, useEffect, useState } from "react";
import { useRecoilValue } from "recoil";
import { sessionState } from "@chainlit/react-client";
import {
  AlertCircle,
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Plus,
  Trash2,
  Pencil,
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
    add: t(`${TEXT}.add`),
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
    editTitle: t(`${TEXT}.editTitle`),
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

async function ask(name, payload) {
  const answer = await callAction({ name, payload });
  const response = answer && answer.response;
  if (!response) throw new Error(`${name}: the chat gave no answer`);
  if (response.error) throw new Error(response.error);
  return response;
}

function ListHeading({ labels, count, onAdd }) {
  return (
    <div className="flex flex-wrap items-center gap-2 font-semibold">
      {labels.title}
      {count !== undefined ? <span className="font-normal text-muted-foreground">{count}</span> : null}
      <Button variant="outline" size="sm" className="ml-auto h-8" onClick={onAdd} data-mcp-add>
        <Plus />{labels.add}
      </Button>
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

  if (editing) {
    return <ServerForm current={editing.current} labels={labels}
      onBack={() => setEditing(null)}
      onSaved={(name) => { setSelected(name); setEditing(null); }}
      onRemoved={() => { setSelected(null); setEditing(null); }} />;
  }

  if (selected) {
    return <ServerCard name={selected} labels={labels} onBack={() => setSelected(null)}
      onEdit={() => setEditing({ current: selected })} />;
  }

  const heading = <ListHeading labels={labels} count={rows ? rows.length : undefined}
    onAdd={() => setEditing({ current: null })} />;

  if (error) {
    return (
      <div className="flex w-full flex-col gap-2 text-sm" data-mcp-servers>
        {heading}
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
        {heading}
        <div className="text-muted-foreground">{labels.empty}</div>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-2 text-sm" data-mcp-servers>
      {heading}
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
            <dd>{({ none: labels.authNone, bearer: labels.authBearer, local: labels.authLocal, proxy: labels.authProxy })[detail.row.auth]}</dd>
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

function ServerForm({ current, labels, onBack, onSaved, onRemoved }) {
  const [form, setForm] = useState({ name: "", transport: "streamable-http", url: "", auth: "none", token: "", headers: [] });
  const [ready, setReady] = useState(!current);
  const [editable, setEditable] = useState(!current);
  const [hasToken, setHasToken] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground";

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

  const save = async (event) => {
    event.preventDefault();
    if (!editable) return;
    setBusy(true);
    setError("");
    const clean = { ...form, headers: form.headers.map(({ name, value }) => ({ name, value })) };
    try {
      const action = current ? "mcp_update" : "mcp_add";
      const payload = current ? { current, form: clean } : { form: clean };
      const detail = await ask(action, payload);
      onSaved(detail.row.name);
    } catch (failed) {
      setError(failed.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!editable) return;
    setBusy(true);
    setError("");
    try {
      await ask("mcp_remove", { name: current });
      onRemoved();
    } catch (failed) {
      setError(failed.message);
    } finally {
      setBusy(false);
    }
  };

  if (removing) {
    return (
      <div className="flex w-full flex-col gap-2 pb-12 text-sm" data-mcp-form>
        <Button variant="ghost" size="sm" className="-ml-3 h-8 w-fit text-muted-foreground" onClick={() => setRemoving(false)} disabled={busy}>
          <ArrowLeft />{labels.cancel}
        </Button>
        <Card className="flex flex-col gap-3 p-4" data-mcp-remove-confirm>
          <span className="text-base font-semibold">{labels.removeTitle}</span>
          {error ? <Note tone="failure" title={labels.actionFailed} text={error} /> : null}
          <span className="text-muted-foreground">{labels.removeHelp} <strong>{current}</strong></span>
          <input className={inputClass} aria-label={labels.confirmName} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={busy} autoComplete="off" />
          <div className="flex flex-wrap gap-2">
            <Button variant="destructive" size="sm" disabled={busy || confirmation !== current} onClick={remove}>{labels.remove}</Button>
            <Button variant="outline" size="sm" disabled={busy} onClick={() => setRemoving(false)}>{labels.cancel}</Button>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-2 pb-12 text-sm" data-mcp-form data-mcp-readonly={ready && !editable || undefined}>
      <Button variant="ghost" size="sm" className="-ml-3 h-8 w-fit text-muted-foreground" onClick={onBack} disabled={busy}>
        <ArrowLeft />{labels.back}
      </Button>
      <Card className="p-4">
        {ready && !editable ? <p className="mb-4 text-sm text-muted-foreground" data-mcp-role-managed>{labels.roleManaged}</p> : null}
        <form onSubmit={save}>
          <fieldset disabled={busy || !ready || !editable} className={`flex min-w-0 flex-col gap-4 ${ready && !editable ? "text-muted-foreground" : ""}`}>
            <div className="text-base font-semibold">{current ? (editable ? labels.editTitle : labels.settings) : labels.formTitle}</div>
            {error ? <Note tone="failure" title={labels.actionFailed} text={error} /> : null}
            <label className="flex flex-col gap-1.5">
              {labels.name}
              <input className={inputClass} name="mcp-name" value={form.name} onChange={(event) => field("name", event.target.value)} maxLength={64} required />
              <span className="text-xs text-muted-foreground">{labels.nameHint}</span>
            </label>
            <label className="flex flex-col gap-1.5">
              {labels.transport}
              <select className={inputClass} name="mcp-transport" value={form.transport} onChange={(event) => field("transport", event.target.value)}>
                <option value="streamable-http">HTTP</option><option value="sse">SSE</option>
              </select>
            </label>
            <label className="flex flex-col gap-1.5">
              {labels.address}
              <input className={inputClass} name="mcp-url" value={form.url} onChange={(event) => field("url", event.target.value)} required autoComplete="off" spellCheck={false} />
            </label>
            <label className="flex flex-col gap-1.5">
              {labels.auth}
              <select className={inputClass} name="mcp-auth" value={form.auth} onChange={(event) => field("auth", event.target.value)}>
                <option value="none">{labels.authNone}</option><option value="bearer">{labels.authBearer}</option>
                {!editable && form.auth === "local" ? <option value="local">{labels.authLocal}</option> : null}
                {!editable && form.auth === "proxy" ? <option value="proxy">{labels.authProxy}</option> : null}
              </select>
            </label>
            {form.auth === "bearer" ? (
              <label className="flex flex-col gap-1.5">
                {labels.token}
                <input className={inputClass} name="mcp-token" type="password" autoComplete="new-password" value={form.token}
                  onChange={(event) => field("token", event.target.value)} placeholder={hasToken ? labels.tokenSaved : ""} />
              </label>
            ) : null}
            <div className="flex flex-col gap-2">
              <span>{labels.headers}</span>
              {form.headers.map((row, index) => (
                <div key={index} className="flex flex-wrap items-center gap-2" data-mcp-header>
                  <input className={inputClass} aria-label={labels.headerName} value={row.name} onChange={(event) => header(index, "name", event.target.value)} maxLength={64} required />
                  <input className={inputClass} type="password" aria-label={labels.headerValue} autoComplete="new-password" value={row.value}
                    onChange={(event) => header(index, "value", event.target.value)} placeholder={row.saved ? labels.tokenSaved : ""} />
                  <Button type="button" variant="ghost" size="sm" onClick={() => field("headers", form.headers.filter((_, at) => at !== index))} aria-label={labels.headerRemove}>
                    <Trash2 />{labels.headerRemove}
                  </Button>
                </div>
              ))}
              <Button type="button" variant="outline" size="sm" className="h-8 w-fit" onClick={() => field("headers", [...form.headers, { name: "", value: "" }])}>
                <Plus />{labels.headerAdd}
              </Button>
            </div>
            {editable ? <div className="flex flex-wrap gap-2">
              <Button type="submit" size="sm">{busy ? labels.saving : labels.save}</Button>
              <Button type="button" variant="outline" size="sm" onClick={onBack}>{labels.cancel}</Button>
            </div> : null}
          </fieldset>
        </form>
        {current && ready && editable ? (
          <div className="mt-4 border-t pt-4">
            <Button variant="destructive" size="sm" className="w-fit" disabled={busy} onClick={() => setRemoving(true)} data-mcp-remove><Trash2 />{labels.remove}</Button>
          </div>
        ) : null}
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
