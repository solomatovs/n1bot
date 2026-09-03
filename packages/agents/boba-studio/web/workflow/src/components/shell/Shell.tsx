import { type ReactElement, useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useParams } from "react-router-dom";

import { useServices } from "../../app";
import { errorText } from "../Async";
import { ShellDataContext, type ShellData } from "../../hooks/useShellData";
import type { StoredRun, StoredWorkflow } from "../../model/workflow";
import { Panel } from "../../ui";
import { Topbar } from "./Topbar";
import { WorkflowList } from "./WorkflowList";

const LIST_WIDTH_KEY = "studio.list.width";
const LIST_COLLAPSED_KEY = "studio.list.collapsed";
const NARROW_QUERY = "(max-width: 760px)";

function storedCollapsed(): boolean {
  try {
    return window.localStorage.getItem(LIST_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function remember(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // приватное окно: настройка живёт до перезагрузки
  }
}

/** Каркас Studio: топбар, единый список workflow с их запусками, сцена справа.
 *
 * Панель списка живёт в двух режимах: на широком экране её можно свернуть и
 * растянуть за правый край (размер и свёрнутость запоминаются), на узком —
 * это ящик поверх сцены. */
export function Shell(): ReactElement {
  const { api, socket } = useServices();
  const { runId, workflowId } = useParams();
  const [workflows, setWorkflows] = useState<StoredWorkflow[]>([]);
  const [runs, setRuns] = useState<StoredRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const [listOpen, setListOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(storedCollapsed);
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW_QUERY).matches);

  useEffect(() => {
    const media = window.matchMedia(NARROW_QUERY);
    const onChange = (): void => {
      setNarrow(media.matches);
    };

    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);

  const toggleList = useCallback(() => {
    if (window.matchMedia(NARROW_QUERY).matches) {
      setListOpen((current) => !current);
      return;
    }

    setCollapsed((current) => {
      remember(LIST_COLLAPSED_KEY, current ? "0" : "1");
      return !current;
    });
  }, []);



  useEffect(() => {
    let alive = true;
    Promise.all([api.listWorkflows(), api.listRuns(200)]).then(
      ([loadedWorkflows, loadedRuns]) => {
        if (!alive) {
          return;
        }

        setWorkflows(loadedWorkflows);
        setRuns(loadedRuns);
        setError("");
        setLoading(false);
      },
      (failure: unknown) => {
        if (alive) {
          setError(errorText(failure));
          setLoading(false);
        }
      },
    );

    return () => {
      alive = false;
    };
  }, [api, tick]);

  const reload = useCallback(() => {
    setTick((n) => n + 1);
  }, []);

  // списки — лента пользователя: любое изменение с любого инстанса приходит по шине
  useEffect(
    () =>
      socket.onUser((event) => {
        if (
          event.kind === "run_list_changed" ||
          event.kind === "workflow_changed" ||
          event.kind === "workflow_draft_changed"
        ) {
          reload();
        }
      }),
    [socket, reload],
  );

  const data = useMemo<ShellData>(
    () => ({ workflows, runs, loading, error, reload }),
    [workflows, runs, loading, error, reload],
  );

  // на узком экране список — ящик: выбор записи его закрывает
  const closeList = useCallback(() => {
    setListOpen(false);
  }, []);
  useEffect(closeList, [closeList, runId, workflowId]);

  const currentRun = runs.find((run) => run.id === runId) ?? null;
  const workflowOfRun = currentRun === null ? null : currentRun.workflow_id;
  const currentWorkflow =
    workflows.find((item) => item.id === (workflowId ?? workflowOfRun)) ?? null;

  let panelShown = !collapsed;
  if (narrow) {
    panelShown = listOpen;
  }

  return (
    <ShellDataContext.Provider value={data}>
      <div className="shell">
        <Topbar
          run={currentRun}
          workflow={currentWorkflow}
          listOpen={panelShown}
          onToggleList={toggleList}
        />
        <div className="shell__body">
          <Panel
            aria-label="workflows"
            className="list"
            open={listOpen}
            collapsed={collapsed && !narrow}
            narrow={narrow}
            storageKey={LIST_WIDTH_KEY}
          >
            <WorkflowList
              workflows={workflows}
              runs={runs}
              selectedWorkflow={workflowId ?? null}
              selectedRun={runId ?? null}
              onPick={closeList}
            />
          </Panel>
          <Outlet />
        </div>
      </div>
    </ShellDataContext.Provider>
  );
}
