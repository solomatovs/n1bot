import { Square } from "lucide-react";
import { type ReactElement, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useServices } from "../app";
import { RunSocket } from "../api/socket";
import { Async, errorText, type Loadable } from "../components/Async";
import { Inspector } from "../components/Inspector";
import { RunGraph } from "../components/graph/RunGraph";
import { StatusBadge } from "../components/StatusBadge";
import { Timeline } from "../components/Timeline";
import { useClock } from "../hooks/useClock";
import { runFinished } from "../model/status";
import { formatInstant } from "../model/time";
import type { RunSnapshot, StoredRun } from "../model/workflow";
import { describeInitiator } from "./ListPage";

/** Страница запуска: запись из REST, дальше живые снимки по сокету. */
export function RunPage(): ReactElement {
  const { runId } = useParams();
  const { api, urls } = useServices();
  const [run, setRun] = useState<Loadable<StoredRun>>({ kind: "loading" });
  const [notice, setNotice] = useState("");
  const [selectedTask, setSelectedTask] = useState<string | null>(null);

  useEffect(() => {
    if (runId === undefined) {
      return;
    }

    let alive = true;
    api.getRun(runId).then(
      (loaded) => {
        if (alive) {
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

  useEffect(() => {
    if (runId === undefined) {
      return;
    }

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
    };

    return socket.subscribe(runId, applySnapshot, setNotice);
  }, [socket, runId]);

  const stop = useCallback(async () => {
    if (runId === undefined) {
      return;
    }

    const stopped = await api.stop(runId);
    if (!stopped) {
      setNotice("nothing to stop: the run is not live on this instance");
    }
  }, [api, runId]);

  const live = run.kind === "ready" && !runFinished(run.value.status);
  const now = useClock(live);

  return (
    <main className="page page--canvas">
      <Async
        state={run}
        render={(loaded) => (
          <>
            <RunHeader run={loaded} onStop={stop} notice={notice} />
            <div className="canvas">
              <div className="canvas__graph">
                <RunGraph run={loaded.state} selectedTask={selectedTask} onSelectTask={setSelectedTask} />
              </div>
              {selectedTask !== null && <Inspector run={loaded.state} task={selectedTask} />}
            </div>
            <Timeline run={loaded.state} startedAt={loaded.started_at} now={now} />
          </>
        )}
      />
    </main>
  );
}

type HeaderProps = {
  run: StoredRun;
  notice: string;
  onStop: () => Promise<void>;
};

function RunHeader({ run, notice, onStop }: HeaderProps): ReactElement {
  const live = !runFinished(run.status);
  return (
    <section className="run-header">
      <h2 className="section__title">
        {run.workflow_id === null ? (
          run.state.graph.spec.name
        ) : (
          <Link to={`/w/${run.workflow_id}`}>{run.state.graph.spec.name}</Link>
        )}
        <StatusBadge status={run.status} />
        <button type="button" className="btn btn--danger" disabled={!live} onClick={() => void onStop()}>
          <Square size={14} /> Stop
        </button>
      </h2>
      <div className="muted">
        {describeInitiator(run.initiator)} · {run.instance} · started {formatInstant(run.started_at)} ·
        finished {formatInstant(run.finished_at)}
      </div>
      {notice !== "" && <div className="notice notice--error">{notice}</div>}
    </section>
  );
}
