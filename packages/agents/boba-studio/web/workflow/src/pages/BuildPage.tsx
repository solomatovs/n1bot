import { Code2, Eraser, Play, Save, ShieldCheck, Trash2 } from "lucide-react";
import { type ReactElement, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { z } from "zod";

import { ApiError } from "../api/client";
import { useServices } from "../app";
import { Async, errorText } from "../components/Async";
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
import { Button, EmptyState, Toolbar, ToolbarHint, ToolbarLabel, ToolbarSpacer, useToast } from "../ui";
import { Input, TextArea } from "../ui";

/** Пауза перед отправкой черновика: серия правок уходит одной записью. */
const DRAFT_DEBOUNCE_MS = 300;

const LayoutSchema = z.object({
  positions: z.record(z.object({ x: z.number(), y: z.number() })),
});

type Loaded = {
  catalog: ToolCatalog;
  stored: StoredWorkflow;
};

/** Сцена Build: пусто без выбора, иначе билдер выбранного workflow. */
export function BuildPage(): ReactElement {
  const { workflowId } = useParams();
  const { api } = useServices();

  const load = useCallback(async (): Promise<Loaded | null> => {
    if (workflowId === undefined) {
      return null;
    }

    const catalog = await api.catalog();
    return { catalog, stored: await api.getWorkflow(workflowId) };
  }, [api, workflowId]);
  const [loaded] = useLoadable(load);

  if (workflowId === undefined) {
    return (
      <main className="stage stage--build">
        <EmptyState fill title="Build workflows">
          Pick a workflow or a run on the left, or press “+ New workflow”.
        </EmptyState>
      </main>
    );
  }

  return (
    <Async
      state={loaded}
      render={(data) => {
        if (data === null) {
          return <></>;
        }

        return <Builder key={data.stored.id} catalog={data.catalog} stored={data.stored} />;
      }}
    />
  );
}

type BuilderProps = {
  catalog: ToolCatalog;
  stored: StoredWorkflow;
};

/** Черновик главнее сохранённого: он и есть то, что вкладки правят сейчас. */
function initialWorkflow(stored: StoredWorkflow): EditableWorkflow {
  if (stored.draft_spec !== null) {
    return parseSpecText(stored.draft_spec);
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

function initialPositions(stored: StoredWorkflow, workflow: EditableWorkflow, catalog: ToolCatalog): TaskPositions {
  if (stored.draft_spec !== null) {
    return positionsOf(stored.draft_layout, workflow, catalog);
  }

  return positionsOf(stored.layout, workflow, catalog);
}

/** Текст ошибок спеки для всплывашки: каждая строка — одна ошибка. */
function issuesText(issues: SpecIssue[]): string {
  const lines: string[] = [];
  for (const issue of issues) {
    if (issue.where === "") {
      lines.push(issue.message);
      continue;
    }

    lines.push(`${issue.where}: ${issue.message}`);
  }

  return lines.join("\n");
}

function Builder({ catalog, stored }: BuilderProps): ReactElement {
  const { api, socket } = useServices();
  const shell = useShellData();
  const navigate = useNavigate();
  const toast = useToast();
  const [workflow, setWorkflow] = useState<EditableWorkflow>(() => initialWorkflow(stored));
  const [positions, setPositions] = useState<TaskPositions>(() => initialPositions(stored, workflow, catalog));
  const [hasDraft, setHasDraft] = useState(stored.draft_spec !== null);
  const revision = useRef(stored.draft_revision);
  const remote = useRef(false);
  const untouched = useRef(true);
  // workflow закрыт (удалён): отложенная запись черновика не должна его искать
  const closed = useRef(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [yamlMode, setYamlMode] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [issues, setIssues] = useState<SpecIssue[]>([]);

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

  /** Ставит редактор в присланное состояние, не отправляя его черновиком обратно. */
  const applyRemote = useCallback(
    (spec: string, layout: unknown, freshRevision: number, freshDraft: boolean) => {
      const parsed = parseSpecText(spec);
      remote.current = true;
      revision.current = freshRevision;
      setWorkflow(parsed);
      setPositions(positionsOf(layout, parsed, catalog));
      setHasDraft(freshDraft);
    },
    [catalog],
  );

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

    const timer = window.setTimeout(() => {
      if (closed.current) {
        return;
      }

      void api.putWorkflowDraft(stored.id, renderSpecText(workflow), { positions }, socket.id).then(
        (saved) => {
          revision.current = Math.max(revision.current, saved.draft_revision);
          setHasDraft(true);
        },
        (error: unknown) => {
          toast(`draft not shared: ${errorText(error)}`, "error");
        },
      );
    }, DRAFT_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [workflow, positions, api, socket, stored.id, toast]);

  // чужая правка черновика: перечитать строку и применить, если она новее нашей
  useEffect(
    () =>
      socket.onUser((event) => {
        if (event.kind !== "workflow_draft_changed" || event.workflow_id !== stored.id) {
          return;
        }

        if (event.by_sid !== "" && event.by_sid === socket.id) {
          return;
        }

        if (event.revision <= revision.current) {
          return;
        }

        void api.getWorkflow(stored.id).then((fresh) => {
          if (fresh.draft_revision <= revision.current) {
            return;
          }

          if (fresh.draft_spec === null) {
            applyRemote(fresh.spec, fresh.layout, fresh.draft_revision, false);
            return;
          }

          applyRemote(fresh.draft_spec, fresh.draft_layout, fresh.draft_revision, true);
        });
      }),
    [socket, api, stored.id, applyRemote],
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
      toast("yaml applied", "success");
    } catch (error: unknown) {
      if (error instanceof SpecTextError) {
        setIssues([{ code: "yaml", where: "", message: error.message }]);
        toast(error.message, "error");
        return;
      }

      toast(errorText(error), "error");
    }
  }, [yamlText, toast, catalog]);

  const remember = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 400) {
        const parsed = parseIssues(error.detail);
        setIssues(parsed);
        toast(issuesText(parsed), "error");
        return;
      }

      toast(errorText(error), "error");
    },
    [toast],
  );

  const validate = useCallback(async () => {
    try {
      const state = await api.validate(specText());
      setIssues([]);
      toast(`valid: ${state.graph.stages.length} stage(s)`, "success");
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, specText, remember, toast]);

  const save = useCallback(async () => {
    try {
      const saved = await api.saveInto(stored.id, specText(), { positions });
      revision.current = saved.draft_revision;
      setIssues([]);
      setHasDraft(false);
      toast(`saved "${saved.name}"`, "success");
      shell.reload();
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, specText, positions, remember, toast, stored.id, shell]);

  const clearDraft = useCallback(async () => {
    try {
      const fresh = await api.clearWorkflowDraft(stored.id, socket.id);
      applyRemote(fresh.spec, fresh.layout, fresh.draft_revision, false);
      setIssues([]);
      shell.reload();
      toast("draft cleared", "success");
    } catch (error: unknown) {
      toast(errorText(error), "error");
    }
  }, [api, stored.id, socket, applyRemote, shell, toast]);

  const removeWorkflow = useCallback(async () => {
    try {
      closed.current = true;
      await api.remove(stored.id);
      shell.reload();
      toast(`deleted "${workflow.name}"`, "success");
      await navigate("/workflow");
    } catch (error: unknown) {
      closed.current = false;
      toast(errorText(error), "error");
    }
  }, [api, stored.id, shell, toast, navigate, workflow.name]);

  const run = useCallback(async () => {
    try {
      const runId = await api.run(stored.id);
      shell.reload();
      await navigate(`/runs/${runId}`);
    } catch (error: unknown) {
      remember(error);
    }
  }, [api, stored.id, remember, navigate, shell]);

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
      <Toolbar variant="builder">
        <ToolbarLabel>Builder</ToolbarLabel>
        <Input
          value={workflow.name}
          onChange={(event) => {
            setWorkflow({ ...workflow, name: event.target.value });
          }}
          aria-label="workflow name"
        />
        <ToolMenu catalog={catalog} onAdd={addTask} />
        <Button tone={yamlMode ? "signal" : "default"} icon={Code2} onClick={toggleYaml} aria-pressed={yamlMode}>
          YAML
        </Button>
        <ToolbarHint variant="builder">edges: drag then → after · result → args · fd → fd</ToolbarHint>
      </Toolbar>
      <Toolbar variant="builder">
        <Button icon={ShieldCheck} onClick={() => void validate()}>
          Validate
        </Button>
        <Button tone="signal" icon={Save} onClick={() => void save()}>
          Save
        </Button>
        <Button tone="primary" icon={Play} onClick={() => void run()}>
          Run
        </Button>
        <Button icon={Eraser} disabled={!hasDraft} onClick={() => void clearDraft()}>
          Clear
        </Button>
        <ToolbarSpacer />
        <Button tone="danger" icon={Trash2} onClick={() => void removeWorkflow()}>
          Delete
        </Button>
      </Toolbar>
      {yamlMode ? (
        <div className="yaml">
          <TextArea
            className="yaml__text"
            value={yamlText}
            onChange={(event) => {
              setYamlText(event.target.value);
            }}
            spellCheck={false}
            aria-label="workflow yaml"
          />
          <div>
            <Button tone="signal" onClick={applyYaml}>
              Apply YAML
            </Button>
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
              toast("only result → args.*, task → task and fd → fd edges are allowed", "error");
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
