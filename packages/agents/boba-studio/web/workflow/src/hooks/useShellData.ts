import { createContext, useContext } from "react";

import type { StoredRun, StoredWorkflow, WorkflowDraft } from "../model/workflow";

/** Списки каркаса: workflow, черновики и запуски субъекта; reload — после действий. */
export type ShellData = {
  workflows: StoredWorkflow[];
  drafts: WorkflowDraft[];
  runs: StoredRun[];
  loading: boolean;
  error: string;
  reload: () => void;
};

export const ShellDataContext = createContext<ShellData | null>(null);

export function useShellData(): ShellData {
  const data = useContext(ShellDataContext);
  if (data === null) {
    throw new Error("shell data is provided by Shell only");
  }

  return data;
}

export function runsOfWorkflow(runs: StoredRun[], workflowId: string): StoredRun[] {
  return runs.filter((run) => run.workflow_id === workflowId);
}
