import { Code2, Play, Save, ShieldCheck } from "lucide-react";
import { type ReactElement, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import type { StoredWorkflow, ToolCatalog, WorkflowDraft } from "../model/workflow";

const EMPTY: EditableWorkflow = { name: "new-workflow", description: "", tasks: [], edges: [] };

/** Пауза перед отправкой черновика: серия правок уходит одной записью. */
const DRAFT_DEBOUNCE_MS = 300;

/** Ключ черновика: сохранённый workflow по id, новый — по uuid из адреса вкладки. */
function draftKeyOf(workflowId: string | undefined, draftParam: string | null): string {
  if (workflowId !== undefined) {
    return `workflow:${workflowId}`;
  }

  if (draftParam !== null) {
    return `new:${draftParam}`;
  }

  return "";
}

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
  draft: WorkflowDraft | null;
};

/** Сцена Build: пусто без выбора, иначе билдер выбранного или нового workflow. */
export function BuildPage(): ReactElement {
  const { workflowId } = useParams();
  const { api } = useServices();
  const location = useLocation();
  const navigate = useNavigate();
  const isNew = location.pathname.endsWith("/build/new");
  const draftParam = new URLSearchParams(location.search).get("draft");
  const draftKey = draftKeyOf(workflowId, draftParam);

  // новый workflow получает uuid черновика в адресе: вторая вкладка по нему же его и откроет
  useEffect(() => {
    if (isNew && draftParam === null) {
      void navigate(`/build/new?draft=${crypto.randomUUID()}`, { replace: true });
    }
  }, [isNew, draftParam, navigate]);

  const load = useCallback(async (): Promise<Loaded> => {
    const catalog = await api.catalog();
    const draft = draftKey === "" ? null : await api.getDraft(draftKey);
    if (workflowId === undefined) {
      return { catalog, stored: null, draft };
    }

    return { catalog, stored: await api.getWorkflow(workflowId), draft };
  }, [api, workflowId, draftKey]);
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
      render={({ catalog, stored, draft }) => (
        <Builder key={draftKey} catalog={catalog} stored={stored} draft={draft} draftKey={draftKey} />
      )}
    />
  );
}

type BuilderProps = {
  catalog: ToolCatalog;
  stored: StoredWorkflow | null;
  draft: WorkflowDraft | null;
  draftKey: string;
};

/** Черновик главнее сохранённого: он и есть то, что вкладки правят сейчас. */
function initialWorkflow(stored: StoredWorkflow | null, draft: WorkflowDraft | null): EditableWorkflow {
  if (draft !== null) {
    return parseSpecText(draft.spec);
  }

  if (stored === null) {
    return EMPTY;
  }

  return parseSpecText(stored.spec);
}

/** Размеры узлов редактора под раскладку: как их нарисует EditorTaskNode. */
function editorSizes(workflow: EditableWorkflow, catalog: ToolCatalog): TaskSizes {
  const sizes: TaskSizes = {};
  for (const task of workflow.tasks) {
    const data = taskData(task, catalog, workflow.edges, null, "");
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

/** Позиции из раскладки, если в ней есть каждая задача; иначе автораскладка. */
function positionsOf(layout: unknown, workflow: EditableWorkflow, catalog: ToolCatalog): TaskPositions {
  const saved = LayoutSchema.safeParse(layout);
  const names = workflow.tasks.map((task) => task.name);
  if (saved.success && names.every((name) => name in saved.data.positions)) {
    return saved.data.positions;
  }

  return autoPositions(workflow, catalog);
}

function initialPositions(
  stored: StoredWorkflow | null,
  draft: WorkflowDraft | null,
  workflow: EditableWorkflow,
  catalog: ToolCatalog,
): TaskPositions {
  if (draft !== null) {
    return positionsOf(draft.layout, workflow, catalog);
  }

  return positionsOf(stored?.layout ?? null, workflow, catalog);
}

function Builder({ catalog, stored, draft, draftKey }: BuilderProps): ReactElement {
  const { api, socket } = useServices();
  const shell = useShellData();
  const navigate = useNavigate();
  const location = useLocation();
  const [workflow, setWorkflow] = useState<EditableWorkflow>(() => initialWorkflow(stored, draft));
  const [positions, setPositions] = useState<TaskPositions>(() => initialPositions(stored, draft, workflow, catalog));
  const revision = useRef(draft?.revision ?? 0);
  const remote = useRef(false);
  const untouched = useRef(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [yamlMode, setYamlMode] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [issues, setIssues] = useState<SpecIssue[]>([]);
  const [notice, setNotice] = useState(() => noticeOf(location.state));
  const [failed, setFailed] = useState(false);
  const [savedId, setSavedId] = useState<string | null>(stored?.id ?? null);

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

  // своя правка уходит черновиком через паузу; пришедшая с шины обратно не отправляется
  useEffect(() => {
    if (untouched.current) {
      untouched.current = false;
      return;
    }

    if (remote.current) {
      remote.current = false;
      return;
    }

    if (draftKey === "") {
      return;
    }

    const timer = window.setTimeout(() => {
      void api.putDraft(draftKey, renderSpecText(workflow), { positions }, socket.id).then(
        (saved) => {
          revision.current = Math.max(revision.current, saved.revision);
        },
        (error: unknown) => {
          report(`draft not shared: ${errorText(error)}`, true);
        },
      );
    }, DRAFT_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [workflow, positions, api, socket, draftKey, report]);

  // чужая правка черновика: перечитать и применить, если она новее нашей
  useEffect(
    () =>
      socket.onUser((event) => {
        if (event.kind !== "workflow_draft_changed" || event.key !== draftKey) {
          return;
        }

        if (event.by_sid !== "" && event.by_sid === socket.id) {
          return;
        }

        if (event.action === "deleted") {
          if (stored === null) {
            return;
          }

          void api.getWorkflow(stored.id).then((fresh) => {
            const parsed = parseSpecText(fresh.spec);
            remote.current = true;
            revision.current = 0;
            setWorkflow(parsed);
            setPositions(positionsOf(fresh.layout, parsed, catalog));
          });
          return;
        }

        if (event.revision <= revision.current) {
          return;
        }

        void api.getDraft(draftKey).then((fresh) => {
          if (fresh === null || fresh.revision <= revision.current) {
            return;
          }

          const parsed = parseSpecText(fresh.spec);
          remote.current = true;
          revision.current = fresh.revision;
          setWorkflow(parsed);
          setPositions(positionsOf(fresh.layout, parsed, catalog));
        });
      }),
    [socket, api, draftKey, stored, catalog],
  );

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
        // черновик нового workflow свою роль сыграл: дальше вкладки правят сохранённый
        await api.dropDraft(draftKey, socket.id);
        await navigate(`/build/${saved.id}`, { replace: true, state: { notice: text } });
      }
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, specText, positions, remember, report, navigate, stored, shell, draftKey, socket]);

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
              edges={workflow.edges}
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
