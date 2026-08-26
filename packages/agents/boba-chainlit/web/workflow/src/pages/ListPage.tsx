import { Play, Trash2 } from "lucide-react";
import { type ReactElement, useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useServices } from "../app";
import { Async } from "../components/Async";
import { StatusBadge } from "../components/StatusBadge";
import { useLoadable } from "../hooks/useLoadable";
import { formatInstant } from "../model/time";
import type { Initiator, StoredRun, StoredWorkflow } from "../model/workflow";

type Listing = {
  workflows: StoredWorkflow[];
  runs: StoredRun[];
};

export function ListPage(): ReactElement {
  const { api } = useServices();
  const navigate = useNavigate();

  const load = useCallback(async (): Promise<Listing> => {
    const [workflows, runs] = await Promise.all([api.listWorkflows(), api.listRuns()]);
    return { workflows, runs };
  }, [api]);
  const [listing, reload] = useLoadable(load);

  const run = useCallback(
    async (id: number) => {
      const runId = await api.run(id);
      await navigate(`/run/${runId}`);
    },
    [api, navigate],
  );

  const remove = useCallback(
    async (workflow: StoredWorkflow) => {
      if (!window.confirm(`Delete workflow "${workflow.name}"?`)) {
        return;
      }

      await api.remove(workflow.id);
      reload();
    },
    [api, reload],
  );

  return (
    <main className="page">
      <Async
        state={listing}
        render={({ workflows, runs }) => (
          <>
            <WorkflowTable workflows={workflows} onRun={run} onDelete={remove} />
            <RunTable runs={runs} />
          </>
        )}
      />
    </main>
  );
}

type WorkflowTableProps = {
  workflows: StoredWorkflow[];
  onRun: (id: number) => Promise<void>;
  onDelete: (workflow: StoredWorkflow) => Promise<void>;
};

function WorkflowTable({ workflows, onRun, onDelete }: WorkflowTableProps): ReactElement {
  return (
    <section className="section">
      <h2 className="section__title">
        Workflows
        <Link className="btn btn--primary" to="/new">
          New
        </Link>
      </h2>
      {workflows.length === 0 ? (
        <div className="notice">No saved workflows yet.</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Tools</th>
              <th>Updated</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {workflows.map((workflow) => (
              <tr key={workflow.id}>
                <td>
                  <Link to={`/w/${workflow.id}`}>{workflow.name}</Link>
                </td>
                <td className="mono">{workflow.tools.join(", ")}</td>
                <td className="muted">{formatInstant(workflow.updated_at)}</td>
                <td>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void onRun(workflow.id)}
                    title="Run"
                  >
                    <Play size={14} /> Run
                  </button>{" "}
                  <button
                    type="button"
                    className="btn btn--danger"
                    onClick={() => void onDelete(workflow)}
                    title="Delete"
                  >
                    <Trash2 size={14} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function RunTable({ runs }: { runs: StoredRun[] }): ReactElement {
  return (
    <section className="section">
      <h2 className="section__title">Runs</h2>
      {runs.length === 0 ? (
        <div className="notice">No runs yet.</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Workflow</th>
              <th>Status</th>
              <th>Initiator</th>
              <th>Started</th>
              <th>Finished</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <Link to={`/run/${run.id}`}>{run.state.graph.spec.name}</Link>
                </td>
                <td>
                  <StatusBadge status={run.status} />
                </td>
                <td className="muted">{describeInitiator(run.initiator)}</td>
                <td className="muted">{formatInstant(run.started_at)}</td>
                <td className="muted">{formatInstant(run.finished_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function describeInitiator(initiator: Initiator): string {
  switch (initiator.kind) {
    case "chat":
      return "chat";
    case "llm":
      return "llm (chat)";
    case "human":
      return `human (${initiator.via})`;
    case "schedule":
      return `schedule ${initiator.job_id}`;
  }
}
