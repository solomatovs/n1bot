import type { Assert, Extends, Schemas } from "../../api/contract";
import type {
  ConnectionVersion,
  Draft,
  ObjectCard,
  Process,
  ProcessContext,
  RebaseResult,
  Share,
  SharedProcess,
  Snapshot,
  Staleness,
  Sync,
  SyncedConnection,
  Version,
} from "../model/catalog";

/** Сверка zod-моделей страницы с OpenAPI-схемой API на этапе компиляции:
 * разбор на границе остаётся у zod, а расхождение полей ломает сборку.
 * Версии сравниваются без operations. */

export type Contract = [
  Assert<Extends<Snapshot, Schemas["CatalogSnapshot"]>>,
  Assert<Extends<Process, Schemas["Process"]>>,
  Assert<Extends<Draft, Schemas["Draft"]>>,
  Assert<Extends<Version, Omit<Schemas["Version"], "operations">>>,
  Assert<Extends<RebaseResult, Schemas["RebaseResult"]>>,
  Assert<Extends<ProcessContext, Schemas["ProcessContext"]>>,
  Assert<Extends<Staleness, Schemas["Staleness"]>>,
  Assert<Extends<Share, Schemas["Share"]>>,
  Assert<Extends<SharedProcess, Schemas["SharedProcess"]>>,
  Assert<Extends<SyncedConnection, Schemas["SyncedConnection"]>>,
  Assert<Extends<ConnectionVersion, Schemas["ConnectionVersion"]>>,
  Assert<Extends<Sync, Schemas["Sync"]>>,
  Assert<Extends<ObjectCard["ref"], Schemas["ObjectRef"]>>,
];
