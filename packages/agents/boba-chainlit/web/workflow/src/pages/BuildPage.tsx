import { Code2, Play, Save, ShieldCheck } from "lucide-react";
import { type ReactElement, useCallback, useMemo, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { z } from "zod";

import { ApiError } from "../api/client";
import { useServices } from "../app";
import { Async, errorText } from "../components/Async";
import { IssueList } from "../components/build/IssueList";
import { ToolMenu } from "../components/build/ToolMenu";
import { EditorGraph } from "../components/editor/EditorGraph";
import { TaskForm } from "../components/editor/TaskForm";
import { useLoadable } from "../hooks/useLoadable";
import { useShellData } from "../hooks/useShellData";
import { layoutTasks, type TaskPositions, type TaskSizes } from "../model/layout";
import { editorNodeHeight } from "../components/editor/EditorTaskNode";
import { EDITOR_NODE_WIDTH, taskData } from "../components/editor/flow";
import {
  edgeId,
  freeTaskName,
  issueTasks,
  parseIssues,
  parseSpecText,
  removeTask,
  renameTask,
  renderSpecText,
  SpecTextError,
  type EditableEdge,
  type EditableTask,
  type EditableWorkflow,
  type SpecIssue,
} from "../model/spec";
import type { StoredWorkflow, ToolCatalog } from "../model/workflow";

const EMPTY: EditableWorkflow = { name: "new-workflow", description: "", tasks: [], edges: [] };

const LayoutSchema = z.object({
  positions: z.record(z.object({ x: z.number(), y: z.number() })),
});

const NoticeStateSchema = z.object({ notice: z.string() });

function noticeOf(state: unknown): string {
  const parsed = NoticeStateSchema.safeParse(state);
  if (!parsed.success) {
    return "";
  }

  return parsed.data.notice;
}

type Loaded = {
  catalog: ToolCatalog;
  stored: StoredWorkflow | null;
};

/** Сцена Build: пусто без выбора, иначе билдер выбранного или нового workflow. */
export function BuildPage(): ReactElement {
  const { workflowId } = useParams();
  const { api } = useServices();
  const location = useLocation();
  const isNew = location.pathname.endsWith("/build/new");

  const load = useCallback(async (): Promise<Loaded> => {
    const catalog = await api.catalog();
    if (workflowId === undefined) {
      return { catalog, stored: null };
    }

    return { catalog, stored: await api.getWorkflow(Number(workflowId)) };
  }, [api, workflowId]);
  const [loaded] = useLoadable(load);

  if (workflowId === undefined && !isNew) {
    return (
      <main className="stage stage--build">
        <div className="empty" style={{ gridRow: "1 / -1" }}>
          <span className="empty__title">Build workflows</span>
          <span>Pick a workflow on the left or press “+ New workflow”.</span>
        </div>
      </main>
    );
  }

  return (
    <Async
      state={loaded}
      render={({ catalog, stored }) => <Builder key={stored?.id ?? "new"} catalog={catalog} stored={stored} />}
    />
  );
}

type BuilderProps = {
  catalog: ToolCatalog;
  stored: StoredWorkflow | null;
};

function initialWorkflow(stored: StoredWorkflow | null): EditableWorkflow {
  if (stored === null) {
    return EMPTY;
  }

  return parseSpecText(stored.spec);
}

/** Размеры узлов редактора под раскладку: как их нарисует EditorTaskNode. */
function editorSizes(workflow: EditableWorkflow, catalog: ToolCatalog): TaskSizes {
  const sizes: TaskSizes = {};
  for (const task of workflow.tasks) {
    const data = taskData(task, catalog, null, "");
    sizes[task.name] = { width: EDITOR_NODE_WIDTH, height: editorNodeHeight(data) };
  }

  return sizes;
}

function autoPositions(workflow: EditableWorkflow, catalog: ToolCatalog): TaskPositions {
  return layoutTasks(
    workflow.tasks.map((task) => task.name),
    workflow.edges.map((edge) => ({ source: edge.src.task, target: edge.dst.task })),
    editorSizes(workflow, catalog),
  );
}

function initialPositions(
  stored: StoredWorkflow | null,
  workflow: EditableWorkflow,
  catalog: ToolCatalog,
): TaskPositions {
  const saved = stored === null ? null : LayoutSchema.safeParse(stored.layout);
  const names = workflow.tasks.map((task) => task.name);
  if (saved?.success === true && names.every((name) => name in saved.data.positions)) {
    return saved.data.positions;
  }

  return autoPositions(workflow, catalog);
}

function Builder({ catalog, stored }: BuilderProps): ReactElement {
  const { api } = useServices();
  const shell = useShellData();
  const navigate = useNavigate();
  const location = useLocation();
  const [workflow, setWorkflow] = useState<EditableWorkflow>(() => initialWorkflow(stored));
  const [positions, setPositions] = useState<TaskPositions>(() => initialPositions(stored, workflow, catalog));
  const [selected, setSelected] = useState<string | null>(null);
  const [yamlMode, setYamlMode] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [issues, setIssues] = useState<SpecIssue[]>([]);
  const [notice, setNotice] = useState(() => noticeOf(location.state));
  const [failed, setFailed] = useState(false);
  const [savedId, setSavedId] = useState<number | null>(stored?.id ?? null);

  const issuesByTask = useMemo(() => {
    const found = new Map<string, string>();
    for (const issue of issues) {
      for (const task of issueTasks(issue, workflow)) {
        found.set(task, `${found.get(task) ?? ""}${issue.message}\n`);
      }
    }

    return found;
  }, [issues, workflow]);

  const specText = useCallback(() => renderSpecText(workflow), [workflow]);

  const report = useCallback((text: string, isError: boolean) => {
    setNotice(text);
    setFailed(isError);
  }, []);

  const toggleYaml = useCallback(() => {
    if (!yamlMode) {
      setYamlText(specText());
    }

    setYamlMode((current) => !current);
  }, [yamlMode, specText]);

  const applyYaml = useCallback(() => {
    try {
      const parsed = parseSpecText(yamlText);
      setWorkflow(parsed);
      setPositions((current) => {
        const names = parsed.tasks.map((task) => task.name);
        if (names.every((name) => name in current)) {
          return current;
        }

        return autoPositions(parsed, catalog);
      });
      setIssues([]);
      report("yaml applied", false);
    } catch (error: unknown) {
      if (error instanceof SpecTextError) {
        setIssues([{ code: "yaml", where: "", message: error.message }]);
        return;
      }

      report(errorText(error), true);
    }
  }, [yamlText, report, catalog]);

  const remember = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 400) {
        setIssues(parseIssues(error.detail));
        setNotice("");
        return;
      }

      report(errorText(error), true);
    },
    [report],
  );

  const validate = useCallback(async () => {
    try {
      const state = await api.validate(specText());
      setIssues([]);
      report(`valid: ${state.graph.stages.length} stage(s)`, false);
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, specText, remember, report]);

  const save = useCallback(async () => {
    try {
      const saved = await api.save(specText(), { positions });
      const text = `saved "${saved.name}" (id ${saved.id})`;
      setSavedId(saved.id);
      setIssues([]);
      report(text, false);
      shell.reload();
      if (stored === null) {
        await navigate(`/build/${saved.id}`, { replace: true, state: { notice: text } });
      }
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, specText, positions, remember, report, navigate, stored, shell]);

  const run = useCallback(async () => {
    if (savedId === null) {
      report("save the workflow first", true);
      return;
    }

    try {
      const runId = await api.run(savedId);
      shell.reload();
      await navigate(`/observe/${runId}`);
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, savedId, remember, report, navigate, shell]);

  const addTask = useCallback(
    (tool: string) => {
      // обязательные аргументы не предзаполняются: пустое значение прошло бы
      // валидацию, а незаданное сервер подсветит как missing_arg
      const name = freeTaskName(tool, workflow.tasks.map((task) => task.name));
      setWorkflow({ ...workflow, tasks: [...workflow.tasks, { name, tool, args: {}, ports: {} }] });
      setPositions({ ...positions, [name]: { x: 40 + workflow.tasks.length * 30, y: 40 + workflow.tasks.length * 30 } });
      setSelected(name);
    },
    [workflow, positions],
  );

  const changeTask = useCallback((changed: EditableTask) => {
    setWorkflow((current) => ({
      ...current,
      tasks: current.tasks.map((task) => (task.name === changed.name ? changed : task)),
    }));
  }, []);

  const rename = useCallback((from: string, to: string) => {
    setWorkflow((current) => renameTask(current, from, to));
    setPositions((current) => {
      const moved: TaskPositions = {};
      for (const [name, position] of Object.entries(current)) {
        moved[name === from ? to : name] = position;
      }

      return moved;
    });
    setSelected(to);
  }, []);

  const remove = useCallback((name: string) => {
    setWorkflow((current) => removeTask(current, name));
    setSelected((current) => (current === name ? null : current));
  }, []);

  const connect = useCallback((edge: EditableEdge) => {
    setWorkflow((current) => {
      if (current.edges.some((known) => edgeId(known) === edgeId(edge))) {
        return current;
      }

      return { ...current, edges: [...current.edges, edge] };
    });
  }, []);

  const removeEdge = useCallback((id: string) => {
    setWorkflow((current) => ({ ...current, edges: current.edges.filter((edge) => edgeId(edge) !== id) }));
  }, []);

  const move = useCallback((task: string, x: number, y: number) => {
    setPositions((current) => ({ ...current, [task]: { x, y } }));
  }, []);

  const selectedTask = workflow.tasks.find((task) => task.name === selected) ?? null;

  return (
    <main className="stage stage--build">
      <div className="builder">
        <span className="builder__label">Builder</span>
        <input
          className="input input--name"
          value={workflow.name}
          onChange={(event) => {
            setWorkflow({ ...workflow, name: event.target.value });
          }}
          aria-label="workflow name"
        />
        <ToolMenu catalog={catalog} onAdd={addTask} />
        <button type="button" className={`btn${yamlMode ? " btn--signal" : ""}`} onClick={toggleYaml} aria-pressed={yamlMode}>
          <Code2 size={12} /> YAML
        </button>
        <span className="builder__hint">edges: drag then → after · result → args · fd → fd</span>
        <span className="viewbar__spacer" />
        <button type="button" className="btn" onClick={() => void validate()}>
          <ShieldCheck size={12} /> Validate
        </button>
        <button type="button" className="btn btn--signal" onClick={() => void save()}>
          <Save size={12} /> Save
        </button>
        <button type="button" className="btn btn--primary" disabled={savedId === null} onClick={() => void run()}>
          <Play size={12} /> Run
        </button>
      </div>
      <div>
        {notice !== "" && (
          <div className="viewbar">
            <span className={`notice${failed ? " notice--error" : ""}`} data-notice>
              {notice}
            </span>
          </div>
        )}
        <IssueList issues={issues} />
      </div>
      {yamlMode ? (
        <div className="yaml">
          <textarea
            className="input yaml__text"
            value={yamlText}
            onChange={(event) => {
              setYamlText(event.target.value);
            }}
            spellCheck={false}
            aria-label="workflow yaml"
          />
          <div>
            <button type="button" className="btn btn--signal" onClick={applyYaml}>
              Apply YAML
            </button>
          </div>
        </div>
      ) : (
        <div className="view">
          <EditorGraph
            workflow={workflow}
            positions={positions}
            catalog={catalog}
            selected={selected}
            issues={issuesByTask}
            onMove={move}
            onSelect={setSelected}
            onConnect={connect}
            onRemoveEdge={removeEdge}
            onRemoveTask={remove}
            onBadConnection={() => {
              report("only result → args.*, task → task and fd → fd edges are allowed", true);
            }}
          />
          {selectedTask !== null && (
            <TaskForm
              key={selectedTask.name}
              task={selectedTask}
              catalog={catalog}
              taken={workflow.tasks.map((task) => task.name)}
              onChange={changeTask}
              onRename={(to) => {
                rename(selectedTask.name, to);
              }}
              onRemove={() => {
                remove(selectedTask.name);
              }}
              onClose={() => {
                setSelected(null);
              }}
            />
          )}
        </div>
      )}
    </main>
  );
}
