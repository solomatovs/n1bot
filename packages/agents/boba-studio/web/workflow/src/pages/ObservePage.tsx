import { Square } from "lucide-react";
import { type ReactElement, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { useServices } from "../app";
import { Alert } from "../ui/Alert";
import { errorText, type Loadable } from "../components/Async";
import { RunGraph } from "../components/graph/RunGraph";
import { Inspector } from "../components/observe/Inspector";
import { TaskTable } from "../components/observe/TaskTable";
import { Timeline } from "../components/observe/Timeline";
import { Vitals } from "../components/observe/Vitals";
import { Segmented } from "../ui/Segmented";
import { useClock } from "../hooks/useClock";
import { useShellData } from "../hooks/useShellData";
import { runFinished } from "../model/status";
import type { RunSnapshot, StoredRun } from "../model/workflow";
import { Button, EmptyState, Toolbar, ToolbarHint, ToolbarSpacer, useToast } from "../ui";

type View = "grid" | "table" | "timeline";

const VIEWS: { value: View; label: string }[] = [
  { value: "grid", label: "Grid" },
  { value: "table", label: "Table" },
  { value: "timeline", label: "Timeline" },
];

/** Сцена Observe: выбранный запуск живьём — vitals, Grid/Table/Timeline, инспектор. */
export function ObservePage(): ReactElement {
  const { runId } = useParams();
  const { api, socket } = useServices();
  const shell = useShellData();
  const navigate = useNavigate();
  const [run, setRun] = useState<Loadable<StoredRun>>({ kind: "loading" });
  const [view, setView] = useState<View>("grid");
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const toast = useToast();
  const finishedRef = useRef(false);
  // снимок, пришедший раньше первого GET: сокет общий и уже подключён, гонка обычна
  const pendingRef = useRef<RunSnapshot | null>(null);

  useEffect(() => {
    if (runId === undefined) {
      return;
    }

    let alive = true;
    setSelectedTask(null);
    setRun({ kind: "loading" });
    finishedRef.current = false;
    pendingRef.current = null;
    api.getRun(runId).then(
      (loaded) => {
        if (!alive) {
          return;
        }

        let value = loaded;
        const pending = pendingRef.current;
        if (pending !== null && pending.run_id === runId) {
          value = { ...loaded, status: pending.status, state: pending.state };
        }
        finishedRef.current = runFinished(value.status);
        setRun({ kind: "ready", value });
      },
      (error: unknown) => {
        if (alive) {
          setRun({ kind: "error", message: errorText(error) });
        }
      },
    );

    return () => {
      alive = false;
    };
  }, [api, runId]);

  // сокет живёт вместе с подпиской: StrictMode и смена запуска пересоздают оба
  const reloadLists = shell.reload;
  useEffect(() => {
    if (runId === undefined) {
      return;
    }

    // списки перезапрашиваются один раз — на переходе запуска в финальный статус
    const applySnapshot = (snapshot: RunSnapshot): void => {
      setRun((current) => {
        if (current.kind !== "ready") {
          pendingRef.current = snapshot;
          return current;
        }

        return {
          kind: "ready",
          value: { ...current.value, status: snapshot.status, state: snapshot.state },
        };
      });
      const finished = runFinished(snapshot.status);
      if (finished && !finishedRef.current) {
        reloadLists();
      }

      finishedRef.current = finished;
    };

    const report = (text: string): void => {
      toast(text, "error");
    };
    const unsubscribe = socket.subscribe(runId, applySnapshot, report);

    return () => {
      unsubscribe();
    };
  }, [socket, runId, reloadLists, toast]);

  const stop = useCallback(async () => {
    if (runId === undefined) {
      return;
    }

    const outcome = await api.stop(runId);
    if (outcome === "finished") {
      toast("nothing to stop: the run is already finished");
    }
    if (outcome === "accepted") {
      toast("stop requested: the run is executed by another instance");
    }
  }, [api, runId, toast]);

  const rerun = useCallback(async () => {
    if (run.kind !== "ready" || run.value.workflow_id === null) {
      return;
    }

    try {
      const started = await api.run(run.value.workflow_id);
      shell.reload();
      await navigate(`/runs/${started}`);
    } catch (error: unknown) {
      toast(errorText(error), "error");
    }
  }, [api, run, navigate, shell, toast]);

  const live = run.kind === "ready" && !runFinished(run.value.status);
  const now = useClock(live);

  if (runId === undefined) {
    return (
      <main className="stage">
        <EmptyState fill title="Runs">
          Expand a workflow on the left and pick a run.
        </EmptyState>
      </main>
    );
  }

  if (run.kind === "loading") {
    return (
      <main className="stage">
        <EmptyState fill>Loading…</EmptyState>
      </main>
    );
  }

  if (run.kind === "error") {
    return (
      <main className="stage">
        <EmptyState fill>
          <Alert tone="error" title="Run failed">
            {run.message}
          </Alert>
        </EmptyState>
      </main>
    );
  }

  const loaded = run.value;
  return (
    <main className="stage" data-run-status={loaded.status}>
      <Vitals run={loaded} now={now} />
      <Toolbar variant="view">
        <ToolbarHint>{loaded.instance}</ToolbarHint>
        <ToolbarSpacer />
        <Segmented options={VIEWS} value={view} onChange={setView} label="view" />
        <ToolbarSpacer />
        <Button tone="signal" disabled={loaded.workflow_id === null} onClick={() => void rerun()}>
          → Re-run
        </Button>
        <Button tone="danger" icon={Square} disabled={!live} onClick={() => void stop()}>
          Stop
        </Button>
      </Toolbar>
      {view === "grid" && (
        <div className="view">
          <RunGraph run={loaded.state} selectedTask={selectedTask} onSelectTask={setSelectedTask} />
        </div>
      )}
      {view === "table" && <TaskTable run={loaded.state} onSelect={setSelectedTask} />}
      {view === "timeline" && (
        <Timeline run={loaded.state} startedAt={loaded.started_at} now={now} onSelect={setSelectedTask} />
      )}
      {selectedTask !== null && (
        <Inspector
          runId={runId}
          run={loaded.state}
          task={selectedTask}
          onClose={() => {
            setSelectedTask(null);
          }}
        />
      )}
    </main>
  );
}
