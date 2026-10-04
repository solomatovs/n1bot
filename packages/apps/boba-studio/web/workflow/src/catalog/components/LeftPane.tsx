import { ArrowUpCircle, Eye, EyeOff, Plus } from "lucide-react";
import { Fragment, useMemo, useState, type ReactElement, type ReactNode } from "react";

import { PageUrls } from "../../config";
import type { CatalogApi } from "../api/client";
import type { Catalog, Draft, ObjectRef, Process, ProcessNode } from "../model/catalog";
import type { EditActions } from "../model/editing";
import type { PaneTab } from "../model/urlState";
import { Chip, IconButton, List, ListAside, ListName, ListRow, Note, Search, Segmented } from "../../ui";
import "./pane.css";
import { ConnectionsPane } from "./ConnectionsPane";

/** Открытый процесс в панели: черновики и узлы по слоям. Без него панель
 * показывает только список процессов. */
export type OpenProcess = {
  processId: string;
  catalog: Catalog;
  activeId: string | undefined;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
  editing: EditActions | undefined;
  onActivate: (nodeId: string) => void;
  onToggleHidden: (nodeId: string) => void;
};

type Props = {
  api: CatalogApi;
  /** Гость по ссылке: только слои, без процессов, черновиков и подключений. */
  guest: boolean;
  tab: PaneTab;
  onTab: (tab: PaneTab) => void;
  /** Закреплённая полоса действий открытого процесса: на опубликованном —
   * ссылка и настройки, на черновике — публикация, отмена, обновление; на
   * входе в каталог полосы нет. */
  actions: ReactNode;
  /** Все процессы каталога и свои открытые черновики одним плоским списком;
   * открытый процесс или черновик подсвечен. */
  processes: Process[];
  drafts: Draft[];
  currentDraftId: string | undefined;
  /** Плюс у списка процессов: только на входе в каталог, где панель про
   * список, и только с правом на правки; у открытого процесса панель про него. */
  onNewProcess: (() => void) | undefined;
  /** «upgrade all» у списка: все отставшие процессы на последние версии
   * снимков; без права на правки — нет. */
  onUpgradeAll: (() => void) | undefined;
  /** Черновик нового процесса из подключения; без права на правки — нет. */
  onProcessFrom: ((name: string) => void) | undefined;
  /** Открытый процесс; на входе в каталог его нет. */
  open: OpenProcess | undefined;
  onAddConnection: (() => void) | undefined;
  selectedObject: ObjectRef | undefined;
  onSelectObject: (ref: ObjectRef) => void;
};

const TABS: { value: PaneTab; label: string }[] = [
  { value: "process", label: "process" },
  { value: "connections", label: "connections" },
];

/** Левая панель. Вкладка процесса — закреплённая полоса действий, плоский
 * список процессов со своими черновиками под каждым и узлы открытого
 * процесса с поиском и глазом, который прячет узел на холсте; вкладка
 * подключений — полоса «add connection» и деревья снимков, из которых
 * берутся узлы. Гостю по ссылке — только узлы. */
export function LeftPane({
  api,
  guest,
  tab,
  onTab,
  actions,
  processes,
  drafts,
  currentDraftId,
  onNewProcess,
  onUpgradeAll,
  onProcessFrom,
  open,
  onAddConnection,
  selectedObject,
  onSelectObject,
}: Props): ReactElement {
  const shown = guest ? "process" : tab;

  return (
    <div className="pane" data-testid="left-pane" data-tab={shown}>
      {!guest && (
        <div className="pane__bar">
          <Segmented options={TABS} value={tab} onChange={onTab} label="left pane tab" fill />
        </div>
      )}
      {shown === "process" ? (
        <>
          {!guest && actions !== null && (
            <div className="pane__bar pane__actions" data-testid="process-actions">
              {actions}
            </div>
          )}
          <div className="pane__scroll">
            {!guest && (
              <ProcessesGroup
                processes={processes}
                drafts={drafts}
                currentProcessId={currentDraftId === undefined ? open?.processId : undefined}
                currentDraftId={currentDraftId}
                onNew={onNewProcess}
                onUpgradeAll={onUpgradeAll}
              />
            )}
            {open !== undefined && (
              <ProcessList
                catalog={open.catalog}
                nodes={open.catalog.nodes}
                activeId={open.activeId}
                hidden={open.hidden}
                showDiff={open.showDiff}
                onActivate={open.onActivate}
                onToggleHidden={open.onToggleHidden}
              />
            )}
          </div>
        </>
      ) : (
        <ConnectionsPane
          api={api}
          pins={open?.catalog.context.pins ?? {}}
          selected={selectedObject}
          onSelect={onSelectObject}
          draggable={open?.editing !== undefined}
          onAdd={onAddConnection}
          onProcessFrom={onProcessFrom}
        />
      )}
    </div>
  );
}

type ProcessesProps = {
  processes: Process[];
  drafts: Draft[];
  currentProcessId: string | undefined;
  currentDraftId: string | undefined;
  onNew: (() => void) | undefined;
  onUpgradeAll: (() => void) | undefined;
};

/** Плоский список: опубликованный процесс строкой (имя, версия, узлы,
 * отставание от снимков и число проблем последнего upgrade), под ним свои
 * черновики этого процесса с чипом draft, в конце черновики новых процессов;
 * открытая строка подсвечена, плюс в заголовке заводит черновик нового
 * процесса, стрелка — upgrade всех отставших. Чужих черновиков в списке нет. */
function ProcessesGroup({
  processes,
  drafts,
  currentProcessId,
  currentDraftId,
  onNew,
  onUpgradeAll,
}: ProcessesProps): ReactElement {
  const fresh = drafts.filter((draft) => draft.process_id === null);
  const lagging = processes.filter((process) => process.behind).length;

  return (
    <PaneGroup
      title={`processes · ${processes.length}`}
      mark="processes-group"
      data={{ "data-behind": lagging }}
      actions={
        <>
          {onUpgradeAll !== undefined && lagging > 0 && (
            <IconButton
              size="sm"
              ghost
              aria-label={`upgrade all processes (${lagging} behind)`}
              onClick={onUpgradeAll}
              data-testid="upgrade-all"
            >
              <ArrowUpCircle size={12} />
            </IconButton>
          )}
          {onNew !== undefined && (
            <IconButton size="sm" ghost aria-label="new process" onClick={onNew} data-testid="new-process">
              <Plus size={12} />
            </IconButton>
          )}
        </>
      }
    >
      <List mark="processes-list" empty={fresh.length === 0 ? "no processes yet" : undefined}>
        {processes.map((process) => (
          <Fragment key={process.id}>
            <ListRow
              active={process.id === currentProcessId}
              data-process={process.name}
              data-behind={process.behind}
              data-attention={process.attention}
              mark="process-item"
            >
              <ListName to={PageUrls.catalog.process(process.id)} title={process.description === "" ? undefined : process.description}>
                {process.name}
              </ListName>
              <ListAside>
                <UpgradeChips behind={process.behind} attention={process.attention} />
                <Chip tone="muted">{process.latest_version === 0 ? "no versions" : `v${process.latest_version}`}</Chip>
                <Chip tone="muted">
                  {process.nodes} node{process.nodes === 1 ? "" : "s"}
                </Chip>
              </ListAside>
            </ListRow>
            {drafts
              .filter((draft) => draft.process_id === process.id)
              .map((draft) => (
                <DraftRow key={draft.id} draft={draft} active={draft.id === currentDraftId} />
              ))}
          </Fragment>
        ))}
        {fresh.map((draft) => (
          <DraftRow key={draft.id} draft={draft} active={draft.id === currentDraftId} fresh />
        ))}
      </List>
    </PaneGroup>
  );
}

type DraftRowProps = {
  draft: Draft;
  active: boolean;
  /** Черновик нового процесса: без родителя, имя станет именем процесса. */
  fresh?: boolean;
};

function DraftRow({ draft, active, fresh = false }: DraftRowProps): ReactElement {
  return (
    <ListRow
      active={active}
      nested={!fresh}
      data-draft={draft.name}
      data-behind={draft.behind}
      data-attention={draft.attention}
      mark="draft-item"
    >
      <ListName to={PageUrls.catalog.draft(draft.id)}>{draft.name}</ListName>
      <ListAside>
        <UpgradeChips behind={draft.behind} attention={draft.attention} />
        {fresh && <Chip tone="muted">new process</Chip>}
        <Chip tone="draft">draft</Chip>
      </ListAside>
    </ListRow>
  );
}

/** Пометки отставания: «behind» — привязки ниже последних версий снимков,
 * «needs attention · N» — последний upgrade остановлен N проблемами. */
function UpgradeChips({ behind, attention }: { behind: boolean; attention: number }): ReactElement | null {
  if (attention > 0) {
    return (
      <Chip tone="warn" mark="attention-chip">
        needs attention · {attention}
      </Chip>
    );
  }

  if (behind) {
    return (
      <Chip tone="warn" mark="behind-chip">
        behind
      </Chip>
    );
  }

  return null;
}

type ListProps = {
  catalog: Catalog;
  nodes: ProcessNode[];
  activeId: string | undefined;
  hidden: ReadonlySet<string>;
  showDiff: boolean;
  onActivate: (nodeId: string) => void;
  onToggleHidden: (nodeId: string) => void;
};

/** Узлы открытого процесса одним списком с поиском: имя, группа чипом, глаз,
 * который прячет карточку на холсте. */
function ProcessList({ catalog, nodes, activeId, hidden, showDiff, onActivate, onToggleHidden }: ListProps): ReactElement {
  const [query, setQuery] = useState("");

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return nodes.filter((node) => {
      if (needle === "") {
        return true;
      }

      return (
        catalog.label(node.id).toLowerCase().includes(needle) || node.ref.path.join("/").toLowerCase().includes(needle)
      );
    });
  }, [catalog, nodes, query]);

  return (
    <PaneGroup title={`nodes · ${nodes.length}`} mark="nodes-group">
      <>
        <div className="pane__search">
          <Search value={query} onChange={setQuery} label="find a node" placeholder="find a node" />
        </div>
        <List>
          {shown.map((node) => {
            const status = showDiff ? catalog.statusOf("node", node.id) : "unchanged";
            const label = catalog.label(node.id);
            const group = catalog.group(node.group_id);
            return (
              <ListRow
                key={node.id}
                active={node.id === activeId}
                hidden={hidden.has(node.id)}
                status={status}
                stale={catalog.staleOf("node", node.id).length > 0}
                data-node={node.ref.path.join("/")}
                mark="pane-item"
              >
                <ListName
                  onClick={() => {
                    onActivate(node.id);
                  }}
                >
                  {label}
                </ListName>
                <ListAside>
                  {group !== undefined && <Chip tone="muted">{group.name}</Chip>}
                  <IconButton
                    size="sm"
                    ghost
                    aria-label={hidden.has(node.id) ? `show ${label}` : `hide ${label}`}
                    aria-pressed={hidden.has(node.id)}
                    onClick={() => {
                      onToggleHidden(node.id);
                    }}
                  >
                    {hidden.has(node.id) ? <EyeOff size={14} /> : <Eye size={14} />}
                  </IconButton>
                </ListAside>
              </ListRow>
            );
          })}
        </List>
        {shown.length === 0 && (
          <Note pad mark="pane-empty">
            {nodes.length === 0 ? "no nodes yet" : "nothing matches"}
          </Note>
        )}
      </>
    </PaneGroup>
  );
}

type GroupProps = {
  title: ReactElement | string;
  /** Заголовок — имя (моно, без капители), не подпись группы. */
  name?: boolean;
  /** Вложенная группа: слой внутри секции слоёв, без своего отступа. */
  nested?: boolean;
  actions?: ReactElement | false | undefined;
  lead?: ReactElement | undefined;
  data?: Record<string, string | number | boolean | undefined>;
  mark?: string | undefined;
  children?: ReactElement | false | undefined;
};

/** Группа панели: заголовок капителью (или именем) с действиями и список
 * под ним. Единственное место, где существуют классы `pane*`. */
export function PaneGroup({
  title,
  name = false,
  nested = false,
  actions,
  lead,
  data,
  mark,
  children,
}: GroupProps): ReactElement {
  const classes = ["pane__group"];
  if (nested) {
    classes.push("pane__group--nested");
  }

  const titleClass = name ? "pane__group-title pane__group-title--name" : "pane__group-title";

  return (
    <section className={classes.join(" ")} data-testid={mark} {...data}>
      <div className="pane__group-head">
        {lead}
        <span className={titleClass}>{title}</span>
        {actions !== undefined && actions !== false && <span className="pane__group-actions">{actions}</span>}
      </div>
      {children}
    </section>
  );
}
