import type { components } from "./schema";
import type { RunState, StoredRun, StoredWorkflow, TaskState, ToolFacts } from "../model/workflow";

/** Сверка zod-моделей страницы с OpenAPI-схемой API на этапе компиляции:
 * разбор на границе остаётся у zod, а расхождение полей ломает сборку.
 * Итоги инструментов исключены: страница добавляет свой kind `opaque`. */
type Schemas = components["schemas"];
type Extends<A, B> = [A] extends [B] ? true : false;
type Assert<T extends true> = T;

export type Contract = [
  Assert<Extends<StoredWorkflow, Schemas["StoredWorkflow"]>>,
  Assert<Extends<Omit<StoredRun, "state">, Omit<Schemas["StoredRun"], "state">>>,
  Assert<Extends<Omit<TaskState, "result">, Omit<Schemas["TaskState"], "result">>>,
  Assert<Extends<RunState["graph"], Schemas["RunState"]["graph"]>>,
  Assert<Extends<RunState["status"], Schemas["RunState"]["status"]>>,
  Assert<Extends<ToolFacts, Schemas["ToolFacts"]>>,
];
