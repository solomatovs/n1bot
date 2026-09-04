import type { components } from "./schema";
import type {
  ConnectionEntry,
  Draft,
  ObjectCard,
  ProcessContext,
  RebaseResult,
  Snapshot,
  Source,
  Staleness,
  Sync,
  Version,
  View,
  ViewLayout,
} from "../model/catalog";

/** Сверка zod-моделей страницы с OpenAPI-схемой API на этапе компиляции:
 * разбор на границе остаётся у zod, а расхождение полей ломает сборку.
 * Версии сравниваются без operations: страница их не читает. */
type Schemas = components["schemas"];
type Extends<A, B> = [A] extends [B] ? true : false;
type Assert<T extends true> = T;

export type Contract = [
  Assert<Extends<Snapshot, Schemas["CatalogSnapshot"]>>,
  Assert<Extends<Draft, Schemas["Draft"]>>,
  Assert<Extends<View, Schemas["View"]>>,
  Assert<Extends<ViewLayout, Schemas["ViewLayout"]>>,
  Assert<Extends<Version, Omit<Schemas["Version"], "operations">>>,
  Assert<Extends<RebaseResult, Schemas["RebaseResult"]>>,
  Assert<Extends<ProcessContext, Schemas["ProcessContext"]>>,
  Assert<Extends<Staleness, Schemas["Staleness"]>>,
  Assert<Extends<Source, Schemas["Source"]>>,
  Assert<Extends<Sync, Schemas["Sync"]>>,
  Assert<Extends<ConnectionEntry, Schemas["ConnectionEntry"]>>,
  Assert<Extends<ObjectCard["ref"], Schemas["ObjectRef"]>>,
];
