import { Square } from "lucide-react";
import { type ReactElement, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { useServices } from "../app";
import { RunSocket } from "../api/socket";
import { errorText, type Loadable } from "../components/Async";
import { RunGraph } from "../components/graph/RunGraph";
import { Inspector } from "../components/observe/Inspector";
import { TaskTable } from "../components/observe/TaskTable";
import { Timeline } from "../components/observe/Timeline";
import { Vitals } from "../components/observe/Vitals";
import { Segmented } from "../components/Segmented";
import { useClock } from "../hooks/useClock";
import { useShellData } from "../hooks/useShellData";
import { runFinished } from "../model/status";
import type { RunSnapshot, StoredRun } from "../model/workflow";

type View = "grid" | "table" | "timeline";

const VIEWS: { value: View; label: string }[] = [
  { value: "grid", label: "Grid" },
  { value: "table", label: "Table" },
  { value: "timeline", label: "Timeline" },
];

/** Сцена Observe: выбранный запуск живьём — vitals, Grid/Table/Timeline, инспектор. */
export function ObservePage(): ReactElement {
  const { runId } = useParams();
  const { api, urls } = useServices();
  const shell = useShellData();
  const navigate = useNavigate();
  const [run, setRun] = useState<Loadable<StoredRun>>({ kind: "loading" });
  const [view, setView] = useState<View>("grid");
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const finishedRef = useRef(false);

  useEffect(() => {
    if (runId === undefined) {
      return;
    }

    let alive = true;
    setSelectedTask(null);
    setRun({ kind: "loading" });
    finishedRef.current = false;
    api.getRun(runId).then(
      (loaded) => {
        if (alive) {
          finishedRef.current = runFinished(loaded.status);
          setRun({ kind: "ready", value: loaded });
        }
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

  const socket = useMemo(() => new RunSocket(urls), [urls]);
  useEffect(
    () => () => {
      socket.close();
    },
    [socket],
  );

  const reloadLists = shell.reload;
  useEffect(() => {
    if (runId === undefined) {
      return;
    }

    // списки перезапрашиваются один раз — на переходе запуска в финальный статус
    const applySnapshot = (snapshot: RunSnapshot): void => {
      setRun((current) => {
        if (current.kind !== "ready") {
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

    return socket.subscribe(runId, applySnapshot, setNotice);
  }, [socket, runId, reloadLists]);

  const stop = useCallback(async () => {
    if (runId === undefined) {
      return;
    }

    const stopped = await api.stop(runId);
    if (!stopped) {
      setNotice("nothing to stop: the run is not live on this instance");
    }
  }, [api, runId]);

  const rerun = useCallback(async () => {
    if (run.kind !== "ready" || run.value.workflow_id === null) {
      return;
    }

    try {
      const started = await api.run(run.value.workflow_id);
      shell.reload();
      await navigate(`/observe/${started}`);
    } catch (error: unknown) {
      setNotice(errorText(error));
    }
  }, [api, run, navigate, shell]);

  const live = run.kind === "ready" && !runFinished(run.value.status);
  const now = useClock(live);

  if (runId === undefined) {
    return (
      <main className="stage">
        <div className="empty" style={{ gridRow: "1 / -1" }}>
          <span className="empty__title">Observe runs</span>
          <span>Pick a run on the left, or build and run a workflow.</span>
        </div>
      </main>
    );
  }

  if (run.kind === "loading") {
    return (
      <main className="stage">
        <div className="empty" style={{ gridRow: "1 / -1" }}>
          Loading…
        </div>
      </main>
    );
  }

  if (run.kind === "error") {
    return (
      <main className="stage">
        <div className="empty" style={{ gridRow: "1 / -1" }}>
          <span className="notice notice--error">{run.message}</span>
        </div>
      </main>
    );
  }

  const loaded = run.value;
  return (
    <main className="stage" data-run-status={loaded.status}>
      <Vitals run={loaded} now={now} />
      <div className="viewbar">
        <span className="viewbar__hint">{loaded.instance}</span>
        <span className="viewbar__spacer" />
        <Segmented options={VIEWS} value={view} onChange={setView} label="view" />
        <span className="viewbar__spacer" />
        <button type="button" className="btn btn--signal" disabled={loaded.workflow_id === null} onClick={() => void rerun()}>
          → Re-run
        </button>
        <button type="button" className="btn btn--danger" disabled={!live} onClick={() => void stop()}>
          <Square size={12} /> Stop
        </button>
      </div>
      {notice !== "" && <div className="stage__float notice notice--error">{notice}</div>}
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
