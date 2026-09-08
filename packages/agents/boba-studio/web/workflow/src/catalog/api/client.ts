import { z } from "zod";

import {
  ApiError,
  route,
  type HttpTransport,
  type Method,
  type PathParams,
  type PathWith,
  type Query,
} from "../../api/transport";
import {
  AccessSchema,
  ConnectionVersionSchema,
  DraftSchema,
  DraftStateSchema,
  ObjectCardSchema,
  ProcessContextSchema,
  ProcessSchema,
  RebaseResultSchema,
  ShareSchema,
  SharedProcessSchema,
  SnapshotSchema,
  SourceDiffSchema,
  SyncSchema,
  SyncedConnectionSchema,
  TreeNodeSchema,
  UpgradeReportSchema,
  UpgradeRunSchema,
  UpgradeSchema,
  VersionSchema,
  type Access,
  type ConnectionVersion,
  type Draft,
  type DraftState,
  type ObjectCard,
  type ObjectKind,
  type Process,
  type ProcessContext,
  type ProcessSpec,
  type RebaseResult,
  type Share,
  type SharedProcess,
  type Snapshot,
  type SourceDiff,
  type Sync,
  type SyncScope,
  type SyncedConnection,
  type TreeNode,
  type Upgrade,
  type UpgradeReport,
  type UpgradeRun,
  type Version,
} from "../model/catalog";
import type { CatalogOp } from "../model/ops";

const DeletedSchema = z.object({ deleted: z.boolean() });
const ForgottenSchema = z.object({ versions: z.number() });

/** Клиент JSON API каталога: пути из OpenAPI, ответы разбирает zod, обмен
 * ведёт общий HttpTransport страницы. */
export class CatalogApi {
  constructor(private readonly transport: HttpTransport) {}

  access(): Promise<Access> {
    return this.call("get", "/v1/catalog/access", {}, undefined, undefined, AccessSchema);
  }

  // --- процессы ---

  processes(): Promise<Process[]> {
    return this.call("get", "/v1/catalog/processes", {}, undefined, undefined, z.array(ProcessSchema));
  }

  process(processId: string): Promise<Process> {
    return this.call(
      "get",
      "/v1/catalog/processes/{process_id}",
      { process_id: processId },
      undefined,
      undefined,
      ProcessSchema,
    );
  }

  createProcess(spec: ProcessSpec): Promise<Process> {
    return this.call("post", "/v1/catalog/processes", {}, undefined, spec, ProcessSchema);
  }

  updateProcess(processId: string, spec: ProcessSpec): Promise<Process> {
    return this.call(
      "put",
      "/v1/catalog/processes/{process_id}",
      { process_id: processId },
      undefined,
      spec,
      ProcessSchema,
    );
  }

  async deleteProcess(processId: string): Promise<void> {
    await this.call(
      "delete",
      "/v1/catalog/processes/{process_id}",
      { process_id: processId },
      undefined,
      undefined,
      DeletedSchema,
    );
  }

  snapshot(processId: string): Promise<Snapshot> {
    return this.call(
      "get",
      "/v1/catalog/processes/{process_id}/snapshot",
      { process_id: processId },
      undefined,
      undefined,
      SnapshotSchema,
    );
  }

  versions(processId: string): Promise<Version[]> {
    return this.call(
      "get",
      "/v1/catalog/processes/{process_id}/versions",
      { process_id: processId },
      undefined,
      undefined,
      z.array(VersionSchema),
    );
  }

  context(processId: string): Promise<ProcessContext> {
    return this.call(
      "get",
      "/v1/catalog/processes/{process_id}/context",
      { process_id: processId },
      undefined,
      undefined,
      ProcessContextSchema,
    );
  }

  // --- черновики ---

  /** Свои открытые черновики по всем процессам. */
  myDrafts(): Promise<Draft[]> {
    return this.call("get", "/v1/catalog/drafts", {}, undefined, undefined, z.array(DraftSchema));
  }

  /** Черновик процесса; без процесса — черновик нового процесса. */
  createDraft(processId: string | null, name: string): Promise<Draft> {
    return this.call("post", "/v1/catalog/drafts", {}, undefined, { process_id: processId, name }, DraftSchema);
  }

  renameDraft(draftId: string, name: string): Promise<Draft> {
    return this.call("put", "/v1/catalog/drafts/{draft_id}", { draft_id: draftId }, undefined, { name }, DraftSchema);
  }

  draft(draftId: string): Promise<DraftState> {
    return this.call(
      "get",
      "/v1/catalog/drafts/{draft_id}",
      { draft_id: draftId },
      undefined,
      undefined,
      DraftStateSchema,
    );
  }

  discardDraft(draftId: string): Promise<Draft> {
    return this.call(
      "delete",
      "/v1/catalog/drafts/{draft_id}",
      { draft_id: draftId },
      undefined,
      undefined,
      DraftSchema,
    );
  }

  /** Порция операций; 409 с current_seq в payload — черновик ушёл вперёд. */
  appendOps(draftId: string, expectedSeq: number, operations: CatalogOp[]): Promise<DraftState> {
    const body = { expected_seq: expectedSeq, operations };
    return this.call(
      "post",
      "/v1/catalog/drafts/{draft_id}/ops",
      { draft_id: draftId },
      undefined,
      body,
      DraftStateSchema,
    );
  }

  publish(draftId: string): Promise<Version> {
    return this.call(
      "post",
      "/v1/catalog/drafts/{draft_id}/publish",
      { draft_id: draftId },
      undefined,
      undefined,
      VersionSchema,
    );
  }

  rebase(draftId: string, dropConflicts: boolean): Promise<RebaseResult> {
    const body = { drop_conflicts: dropConflicts };
    return this.call(
      "post",
      "/v1/catalog/drafts/{draft_id}/rebase",
      { draft_id: draftId },
      undefined,
      body,
      RebaseResultSchema,
    );
  }

  /** Привязки черновика поднимаются до последних версий снимков. */
  // --- upgrade: задача, как синхронизация ---

  upgradeProcess(processId: string): Promise<UpgradeRun> {
    return this.call(
      "post",
      "/v1/catalog/processes/{process_id}/upgrade",
      { process_id: processId },
      undefined,
      undefined,
      UpgradeRunSchema,
    );
  }

  upgradeDraft(draftId: string): Promise<UpgradeRun> {
    return this.call(
      "post",
      "/v1/catalog/drafts/{draft_id}/upgrade",
      { draft_id: draftId },
      undefined,
      undefined,
      UpgradeRunSchema,
    );
  }

  upgradeAll(): Promise<UpgradeRun> {
    return this.call("post", "/v1/catalog/processes/upgrade", {}, undefined, undefined, UpgradeRunSchema);
  }

  upgradeRun(runId: string): Promise<UpgradeRun> {
    return this.call("get", "/v1/catalog/upgrades/{run_id}", { run_id: runId }, undefined, undefined, UpgradeRunSchema);
  }

  cancelUpgrade(runId: string): Promise<UpgradeRun> {
    return this.call(
      "delete",
      "/v1/catalog/upgrades/{run_id}",
      { run_id: runId },
      undefined,
      undefined,
      UpgradeRunSchema,
    );
  }

  upgradeReport(runId: string): Promise<UpgradeReport> {
    return this.call(
      "get",
      "/v1/catalog/upgrades/{run_id}/report",
      { run_id: runId },
      undefined,
      undefined,
      UpgradeReportSchema,
    );
  }

  /** Последние запуски, касающиеся процесса или черновика (свои и по всем). */
  upgradeRuns(processId: string | undefined, draftId: string | undefined): Promise<UpgradeRun[]> {
    const query: Query = {};
    if (processId !== undefined) {
      query.process_id = processId;
    }
    if (draftId !== undefined) {
      query.draft_id = draftId;
    }

    return this.call("get", "/v1/catalog/upgrades", {}, query, undefined, z.array(UpgradeRunSchema));
  }

  /** Последний upgrade процесса; без записей — undefined. */
  async lastUpgrade(processId: string): Promise<Upgrade | undefined> {
    return this.optional(
      this.call(
        "get",
        "/v1/catalog/processes/{process_id}/upgrade",
        { process_id: processId },
        undefined,
        undefined,
        UpgradeSchema,
      ),
    );
  }

  async lastDraftUpgrade(draftId: string): Promise<Upgrade | undefined> {
    return this.optional(
      this.call(
        "get",
        "/v1/catalog/drafts/{draft_id}/upgrade",
        { draft_id: draftId },
        undefined,
        undefined,
        UpgradeSchema,
      ),
    );
  }

  /** Ответ 404 — «записи нет», а не ошибка. */
  private async optional<T>(request: Promise<T>): Promise<T | undefined> {
    try {
      return await request;
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 404) {
        return undefined;
      }

      throw error;
    }
  }

  draftContext(draftId: string): Promise<ProcessContext> {
    return this.call(
      "get",
      "/v1/catalog/drafts/{draft_id}/context",
      { draft_id: draftId },
      undefined,
      undefined,
      ProcessContextSchema,
    );
  }

  // --- ссылки на просмотр ---

  shares(processId: string): Promise<Share[]> {
    return this.call(
      "get",
      "/v1/catalog/processes/{process_id}/shares",
      { process_id: processId },
      undefined,
      undefined,
      z.array(ShareSchema),
    );
  }

  share(processId: string): Promise<Share> {
    return this.call(
      "post",
      "/v1/catalog/processes/{process_id}/shares",
      { process_id: processId },
      undefined,
      undefined,
      ShareSchema,
    );
  }

  revokeShare(token: string): Promise<Share> {
    return this.call("delete", "/v1/catalog/shares/{token}", { token }, undefined, undefined, ShareSchema);
  }

  /** Опубликованный процесс по ссылке: без входа и прав на каталог. */
  shared(token: string): Promise<SharedProcess> {
    return this.call("get", "/v1/catalog/shared/{token}", { token }, undefined, undefined, SharedProcessSchema);
  }

  sharedObject(token: string, nodeId: string): Promise<ObjectCard> {
    const params = { token, node_id: nodeId };
    return this.call(
      "get",
      "/v1/catalog/shared/{token}/nodes/{node_id}/object",
      params,
      undefined,
      undefined,
      ObjectCardSchema,
    );
  }

  // --- снимки подключений и синхронизация ---

  sourceKinds(): Promise<string[]> {
    return this.call("get", "/v1/catalog/source-kinds", {}, undefined, undefined, z.array(z.string()));
  }

  /** Подключения с версиями снимка: имя и вид на момент последнего снятия. */
  synced(): Promise<SyncedConnection[]> {
    return this.call("get", "/v1/catalog/synced", {}, undefined, undefined, z.array(SyncedConnectionSchema));
  }

  connectionVersions(connectionId: string): Promise<ConnectionVersion[]> {
    const params = { connection_id: connectionId };
    return this.call(
      "get",
      "/v1/catalog/connections/{connection_id}/versions",
      params,
      undefined,
      undefined,
      z.array(ConnectionVersionSchema),
    );
  }

  /** Все версии снимка подключения; 409 — оно стоит в узлах процессов. */
  async forgetVersions(connectionId: string): Promise<number> {
    const params = { connection_id: connectionId };
    const forgotten = await this.call(
      "delete",
      "/v1/catalog/connections/{connection_id}/versions",
      params,
      undefined,
      undefined,
      ForgottenSchema,
    );
    return forgotten.versions;
  }

  /** Дети узла дерева версии; version < 0 — последняя. */
  connectionTree(connectionId: string, version: number, path: string[]): Promise<TreeNode[]> {
    const params = { connection_id: connectionId };
    const query: Query = { version, path };
    return this.call(
      "get",
      "/v1/catalog/connections/{connection_id}/tree",
      params,
      query,
      undefined,
      z.array(TreeNodeSchema),
    );
  }

  connectionObject(connectionId: string, version: number, kind: ObjectKind, path: string[]): Promise<ObjectCard> {
    const params = { connection_id: connectionId };
    const query: Query = { version, kind, path };
    return this.call(
      "get",
      "/v1/catalog/connections/{connection_id}/object",
      params,
      query,
      undefined,
      ObjectCardSchema,
    );
  }

  connectionDiff(connectionId: string, oldVersion: number, newVersion: number): Promise<SourceDiff> {
    const params = { connection_id: connectionId };
    const query: Query = { old: oldVersion, new: newVersion };
    return this.call("get", "/v1/catalog/connections/{connection_id}/diff", params, query, undefined, SourceDiffSchema);
  }

  connectionSyncs(connectionId: string): Promise<Sync[]> {
    return this.call(
      "get",
      "/v1/catalog/connections/{connection_id}/syncs",
      { connection_id: connectionId },
      undefined,
      undefined,
      z.array(SyncSchema),
    );
  }

  startSync(connectionId: string, scope: SyncScope): Promise<Sync> {
    return this.call(
      "post",
      "/v1/catalog/connections/{connection_id}/syncs",
      { connection_id: connectionId },
      undefined,
      scope,
      SyncSchema,
    );
  }

  sync(syncId: string): Promise<Sync> {
    return this.call("get", "/v1/catalog/syncs/{sync_id}", { sync_id: syncId }, undefined, undefined, SyncSchema);
  }

  cancelSync(syncId: string): Promise<Sync> {
    return this.call("delete", "/v1/catalog/syncs/{sync_id}", { sync_id: syncId }, undefined, undefined, SyncSchema);
  }

  private call<M extends Method, P extends PathWith<M>, T>(
    method: M,
    path: P,
    params: PathParams<P>,
    query: Query | undefined,
    body: unknown,
    schema: z.ZodType<T>,
  ): Promise<T> {
    return this.transport.call(method, route(path, params, query), body, schema);
  }
}
