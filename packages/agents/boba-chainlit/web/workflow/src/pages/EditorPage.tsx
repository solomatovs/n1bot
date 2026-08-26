import { Play, Save, ShieldCheck } from "lucide-react";
import { type ReactElement, useCallback, useMemo, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { z } from "zod";

import { ApiError } from "../api/client";
import { useServices } from "../app";
import { Async, errorText } from "../components/Async";
import { EditorGraph } from "../components/editor/EditorGraph";
import { IssueList } from "../components/editor/IssueList";
import { Palette } from "../components/editor/Palette";
import { TaskForm } from "../components/editor/TaskForm";
import { useLoadable } from "../hooks/useLoadable";
import { layoutTasks, type TaskPositions } from "../model/layout";
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

type Tab = "graph" | "yaml";

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

/** Редактор: граф и YAML одной модели; Validate/Save/Run — через REST. */
export function EditorPage(): ReactElement {
  const { workflowId } = useParams();
  const { api } = useServices();

  const load = useCallback(async (): Promise<Loaded> => {
    const catalog = await api.catalog();
    if (workflowId === undefined) {
      return { catalog, stored: null };
    }

    return { catalog, stored: await api.getWorkflow(Number(workflowId)) };
  }, [api, workflowId]);
  const [loaded] = useLoadable(load);

  return (
    <Async
      state={loaded}
      render={({ catalog, stored }) => <Editor key={stored?.id ?? "new"} catalog={catalog} stored={stored} />}
    />
  );
}

type EditorProps = {
  catalog: ToolCatalog;
  stored: StoredWorkflow | null;
};

function initialWorkflow(stored: StoredWorkflow | null): EditableWorkflow {
  if (stored === null) {
    return EMPTY;
  }

  return parseSpecText(stored.spec);
}

function initialPositions(stored: StoredWorkflow | null, workflow: EditableWorkflow): TaskPositions {
  const saved = stored === null ? null : LayoutSchema.safeParse(stored.layout);
  const names = workflow.tasks.map((task) => task.name);
  if (saved?.success === true && names.every((name) => name in saved.data.positions)) {
    return saved.data.positions;
  }

  return layoutTasks(
    names,
    workflow.edges.map((edge) => ({ source: edge.src.task, target: edge.dst.task })),
  );
}

function Editor({ catalog, stored }: EditorProps): ReactElement {
  const { api } = useServices();
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<EditableWorkflow>(() => initialWorkflow(stored));
  const [positions, setPositions] = useState<TaskPositions>(() => initialPositions(stored, workflow));
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("graph");
  const [yamlText, setYamlText] = useState("");
  const [issues, setIssues] = useState<SpecIssue[]>([]);
  // после первого Save страница переезжает на /w/{id} и монтируется заново:
  // сообщение о сохранении приезжает состоянием перехода
  const location = useLocation();
  const [notice, setNotice] = useState(() => noticeOf(location.state));
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

  const showTab = useCallback(
    (next: Tab) => {
      if (next === "yaml") {
        setYamlText(specText());
      }

      setTab(next);
    },
    [specText],
  );

  const applyYaml = useCallback(() => {
    try {
      const parsed = parseSpecText(yamlText);
      setWorkflow(parsed);
      setPositions((current) => {
        const names = parsed.tasks.map((task) => task.name);
        if (names.every((name) => name in current)) {
          return current;
        }

        return layoutTasks(
          names,
          parsed.edges.map((edge) => ({ source: edge.src.task, target: edge.dst.task })),
        );
      });
      setIssues([]);
      setNotice("yaml applied");
    } catch (error: unknown) {
      if (error instanceof SpecTextError) {
        setIssues([{ code: "yaml", where: "", message: error.message }]);
        return;
      }

      setNotice(errorText(error));
    }
  }, [yamlText]);

  const remember = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 400) {
      setIssues(parseIssues(error.detail));
      setNotice("");
      return;
    }

    setNotice(errorText(error));
  }, []);

  const validate = useCallback(async () => {
    try {
      const state = await api.validate(specText());
      setIssues([]);
      setNotice(`valid: ${state.graph.stages.length} stage(s)`);
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, specText, remember]);

  const save = useCallback(async () => {
    try {
      const saved = await api.save(specText(), { positions });
      const text = `saved "${saved.name}" (id ${saved.id})`;
      setSavedId(saved.id);
      setIssues([]);
      setNotice(text);
      if (stored === null) {
        await navigate(`/w/${saved.id}`, { replace: true, state: { notice: text } });
      }
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, specText, positions, remember, navigate, stored]);

  const run = useCallback(async () => {
    if (savedId === null) {
      setNotice("save the workflow first");
      return;
    }

    try {
      const runId = await api.run(savedId);
      await navigate(`/run/${runId}`);
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, savedId, remember, navigate]);

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

  const changeTask = useCallback(
    (changed: EditableTask) => {
      setWorkflow((current) => ({
        ...current,
        tasks: current.tasks.map((task) => (task.name === changed.name ? changed : task)),
      }));
    },
    [],
  );

  const rename = useCallback(
    (from: string, to: string) => {
      setWorkflow((current) => renameTask(current, from, to));
      setPositions((current) => {
        const moved: TaskPositions = {};
        for (const [name, position] of Object.entries(current)) {
          moved[name === from ? to : name] = position;
        }

        return moved;
      });
      setSelected(to);
    },
    [],
  );

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
    <main className="page page--canvas">
      <section className="run-header">
        <h2 className="section__title">
          <input
            className="input input--title"
            value={workflow.name}
            onChange={(event) => {
              setWorkflow({ ...workflow, name: event.target.value });
            }}
            aria-label="workflow name"
          />
          <span className="tabs">
            <button type="button" className={`btn ${tab === "graph" ? "btn--active" : ""}`} onClick={() => { showTab("graph"); }}>
              Graph
            </button>
            <button type="button" className={`btn ${tab === "yaml" ? "btn--active" : ""}`} onClick={() => { showTab("yaml"); }}>
              YAML
            </button>
          </span>
          <span className="header__spacer" />
          <button type="button" className="btn" onClick={() => void validate()}>
            <ShieldCheck size={14} /> Validate
          </button>
          <button type="button" className="btn btn--primary" onClick={() => void save()}>
            <Save size={14} /> Save
          </button>
          <button type="button" className="btn" disabled={savedId === null} onClick={() => void run()}>
            <Play size={14} /> Run
          </button>
        </h2>
        {notice !== "" && <div className="notice">{notice}</div>}
        <IssueList issues={issues} />
      </section>

      {tab === "graph" ? (
        <div className="canvas canvas--editor">
          <Palette catalog={catalog} onAdd={addTask} />
          <div className="canvas__graph">
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
                setNotice("only result → args.*, task → task and fd → fd edges are allowed");
              }}
            />
          </div>
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
            />
          )}
        </div>
      ) : (
        <div className="yaml-tab">
          <textarea
            className="input mono yaml-tab__text"
            value={yamlText}
            onChange={(event) => {
              setYamlText(event.target.value);
            }}
            spellCheck={false}
            aria-label="workflow yaml"
          />
          <div className="yaml-tab__actions">
            <button type="button" className="btn btn--primary" onClick={applyYaml}>
              Apply YAML
            </button>
          </div>
        </div>
      )}
      <div />
    </main>
  );
}
