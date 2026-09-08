import type { components } from "./schema";
import type { ConnectionView, Me, ProfileView, SignInProviders } from "../model/account";
import type { RunState, StoredRun, StoredWorkflow, StudioField, TaskState, ToolFacts } from "../model/workflow";

/** Сверка zod-моделей страницы с OpenAPI-схемой API на этапе компиляции:
 * разбор на границе остаётся у zod, а расхождение полей ломает сборку.
 * Итоги инструментов (result задачи, display поля) исключены: страница их
 * по kind не разбирает, показ приходит готовым StudioView. */
export type Schemas = components["schemas"];
export type Extends<A, B> = [A] extends [B] ? true : false;
export type Assert<T extends true> = T;

export type Contract = [
  Assert<Extends<StoredWorkflow, Schemas["StoredWorkflow"]>>,
  Assert<Extends<Omit<StoredRun, "state">, Omit<Schemas["StoredRun"], "state">>>,
  Assert<Extends<Omit<TaskState, "result">, Omit<Schemas["TaskState"], "result">>>,
  Assert<Extends<RunState["graph"], Schemas["RunState"]["graph"]>>,
  Assert<Extends<RunState["status"], Schemas["RunState"]["status"]>>,
  Assert<Extends<Omit<ToolFacts, "args">, Omit<Schemas["ToolFacts"], "args">>>,
  Assert<Extends<Omit<StudioField, "display">, Omit<Schemas["StudioField"], "display">>>,
  Assert<Extends<Me, Schemas["Me"]>>,
  Assert<Extends<ProfileView, Schemas["ProfileView"]>>,
  Assert<Extends<SignInProviders, Schemas["SignInProviders"]>>,
  Assert<Extends<Omit<ConnectionView, "profile">, Omit<Schemas["ConnectionView"], "profile">>>,
];
