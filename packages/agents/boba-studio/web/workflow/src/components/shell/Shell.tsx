import { type ReactElement, useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useParams } from "react-router-dom";

import { useServices } from "../../app";
import { errorText } from "../Async";
import { ShellDataContext, type ShellData } from "../../hooks/useShellData";
import type { StoredRun, StoredWorkflow } from "../../model/workflow";
import { Rail } from "./Rail";
import { RunList } from "./RunList";
import { Topbar } from "./Topbar";
import { WorkflowList } from "./WorkflowList";

export type Mode = "observe" | "build";

type Props = {
  mode: Mode;
};

/** Каркас Studio: топбар, рейл режимов, список слева, сцена справа. */
export function Shell({ mode }: Props): ReactElement {
  const { api, socket } = useServices();
  const { runId, workflowId } = useParams();
  const [workflows, setWorkflows] = useState<StoredWorkflow[]>([]);
  const [runs, setRuns] = useState<StoredRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const [listOpen, setListOpen] = useState(false);

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
        if (event.kind === "run_list_changed" || event.kind === "workflow_changed") {
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
  useEffect(closeList, [closeList, runId, workflowId, mode]);

  const currentRun = runs.find((run) => run.id === runId) ?? null;
  const currentWorkflow = workflows.find((item) => String(item.id) === workflowId) ?? null;

  return (
    <ShellDataContext.Provider value={data}>
      <div className="shell">
        <Topbar
          mode={mode}
          run={currentRun}
          workflow={currentWorkflow}
          listOpen={listOpen}
          onToggleList={() => {
            setListOpen((current) => !current);
          }}
        />
        <div className="shell__body">
          <Rail mode={mode} />
          {mode === "observe" ? (
            <RunList runs={runs} selected={runId ?? null} open={listOpen} onPick={closeList} />
          ) : (
            <WorkflowList
              workflows={workflows}
              runs={runs}
              selected={workflowId ?? null}
              open={listOpen}
              onPick={closeList}
            />
          )}
          <Outlet />
        </div>
      </div>
    </ShellDataContext.Provider>
  );
}
