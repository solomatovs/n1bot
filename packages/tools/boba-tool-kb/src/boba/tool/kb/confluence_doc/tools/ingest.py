"""Tool `confluence_doc_ingest_paths` + `ConfluenceDocIngestConfig`.

Индексация загруженных пользователем `.md`-файлов KbDoc-формата
(плоский `key: value`-хедер до `---` + body) из текущего workspace'а в KB-
коллекцию `kb_confluence_doc`. Хедер строгий: обязательны `source` / `title`
/ `page_id` / `space` — файл без них считается невалидным (`KbDocFormatError`
→ попадает в `failed`, не индексируется). Принимает
workspace-relative пути файлов и/или папок (chainlit upload-attachment'ы
лежат под `upload/<name>`) и гонит через тот же pipeline, что и operator
CLI `boba.tool.kb.cli.confluence_doc.ingest`. Чтение файлов идёт через
`ProjectWorkspaceShell.read_binary` — host-путь наружу не уходит.

Конфиг-секция: `[tool.kb.confluence_doc.ingest]` (store/embedding/chunker/
collection).

Пример строки `kb_chunks`, которую пишет этот tool (один чанк = одна
строка; пишется в `PostgresChunkStore.upsert`). Файл `upload/runbook.md`,
`workspace_id=sess-7f3a`, KbDoc-хедер с `**title:**` и `**source_url:**`:

    chunk_id       = "a1b2c3d4e5f6:0"   # digest(anchor)[:N] + ":" + chunk_index
    collection     = "kb_confluence_doc" # cfg.collection
    source_id      = "ws:sess-7f3a:upload/runbook.md"   # WorkspaceWalkRequestSource
    chunk_index    = 0                   # 0-based, проставляет chunker
    content_hash   = "<hex-digest>"      # ec.content_hash.to_wire()
    raw_content    = "<исходный markdown чанка>"
    format_content = "<нормализованный текст>"   # идёт в tsv + embedding
    embedding      = [0.0123, -0.0456, ...]       # vector, dim = dim embedder'а
    tags           = {}                  # text[]; KbDoc обычно без тэгов
    updated_at     = now()

`metadata` — jsonb, это `ec.metadata.to_wire()` (typed `Metadata` →
плоский `dict[str, str]`, все значения — строки). Ключи namespace'нуты
по слою pipeline, который их проставил:

    {
      "transport.fs.path":    "upload/runbook.md",   # WorkspaceWalkRequestSource
      "transport.fs.name":    "runbook.md",          # WorkspaceWalkRequestSource
      "transport.fs.size":    "2048",                # WorkspaceTransport
      "transport.fs.suffix":  "md",                  # WorkspaceTransport
      "transport.mtime":      "1715342400.0",        # WorkspaceTransport
      "reader.doc_type":      "kbdoc",               # KbDocReader (DOC_TYPE)
      "reader.page_title":    "Postgres Runbook",    # KbDocReader (из title:)
      "source_url":           "https://wiki/runbook",# KbDocReader (из source:)
      "confluence.page_id":   "950276",              # KbDocReader (из page_id:)
      "confluence.space_key": "PAAS",                # KbDocReader (из space:)
      "section.heading.path": "Backup > PITR",       # StructuralChunker
      "section.anchor":       "backup-pitr"          # если у секции есть anchor
    }

KB-документы оператора — выгрузки Confluence, поэтому этот ingest сохраняет
header'ы под ТЕМИ ЖЕ wire-ключами, что и прямая confluence-индексация
(`source_url`, `confluence.page_id`, `confluence.space_key`;
title→`reader.page_title`, anchor→`section.anchor`). За счёт этого search
читает оба источника 1:1 без сведе́ния: tool `kb_confluence_doc_search` (как и
`kb_confluence_search`, единый интерфейс с параметром `method`=`vector`|`fts`)
возвращает `TableResult` — плоскую таблицу с колонками `page_title` /
`source_url` / `anchor` / `page_id` / `heading_path` / `space` + `tags` +
служебные `id`/`distance`/`link`/`snippet`.

Системные поля (`source_id`, `chunk_index`, `content_hash`) и `tags`
живут отдельными колонками; остальное — внутри `metadata` jsonb. Набор
native-ключей `metadata` зависит от source: confluence-ingest кладёт вместо
`transport.fs.*` ключи `confluence.page_id` / `confluence.space_key` /
`confluence.version` и т.п. (см. `boba.tool.kb.confluence.keys`).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, LLMStringList
from boba.tool.kb.confluence_doc._ingest_common import run_confluence_doc_ingest
from boba.tool.kb.confluence_doc._workspace_indexing import (
    WorkspaceTransport,
    WorkspaceWalkRequestSource,
)
from boba.tool.kb.core.chunker_factory import build_chunker
from boba.tool.kb.core.chunker_params import ChunkerParams
from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["ConfluenceDocIngestConfig", "confluence_doc_ingest_paths"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_doc_ingest_paths")


class ConfluenceDocIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_doc_ingest_paths`.

    Config-секция: `[tool.kb.confluence_doc.ingest]`. Operator-controlled поля:
    store/embedding/chunker/collection. LLM выбирает только `paths` и
    `prune_missing` в tool-вызове.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence_doc.ingest",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
        ),
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    collection: str = Field(
        default="kb_confluence_doc",
        min_length=1,
        max_length=255,
        description="Target-коллекция в `kb_chunks`.",
    )


@tool
def confluence_doc_ingest_paths(
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[ConfluenceDocIngestConfig, FromConfig()],
    paths: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Workspace-relative пути файлов или папок с KbDoc `.md`. "
                'Передавай JSON-массив строк: `["upload/a.md", "upload/b.md"]` '
                'или `["upload"]` (папка обходится рекурсивно). Файлы, '
                "загруженные пользователем через chainlit, лежат под "
                "`upload/<имя>` — эти пути ты получаешь из user-сообщения "
                "в блоке `[Прикреплённые файлы (workspace-relative): ...]`."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди файлов, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует KbDoc `.md`-файлы из workspace'а в KB.

    Возвращает JSON `{collection, indexed, skipped_unchanged, pruned, failed,
    paths}`. Не-`.md` файлы игнорируются. Несуществующие пути логируются
    warning'ом и пропускаются.
    """
    chunk_store = PostgresChunkStore(cfg=cfg.store)
    collections_store = PostgresCollectionsStore(cfg=cfg.store)
    embedder = build_embedder(cfg.embedding)
    chunker = build_chunker(cfg.chunker)

    result = run_confluence_doc_ingest(
        request_source=WorkspaceWalkRequestSource(
            shell=shell,
            paths=list(paths),
            include=["*.md"],
        ),
        transport=WorkspaceTransport(shell=shell),
        chunk_store=chunk_store,
        collections_store=collections_store,
        embedder=embedder,
        chunker=chunker,
        collection=cfg.collection,
        prune_missing=prune_missing,
        pipeline_id=_PIPELINE_ID,
    )
    return {"paths": list(paths), **result}
