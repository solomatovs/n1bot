"""Tool `kbdoc_ingest_paths` + `KbdocIngestConfig`.

Индексация загруженных пользователем `.md`-файлов KbDoc-формата
(`**key:** value`-хедер + body) из текущего workspace'а в KB. Принимает
workspace-relative пути файлов и/или папок (chainlit upload-attachment'ы
лежат под `upload/<name>`) и гонит через тот же pipeline, что и operator
CLI `boba.tool.kb.cli.kbdoc.ingest`. Чтение файлов идёт через
`ProjectWorkspaceShell.read_binary` — host-путь наружу не уходит.

Конфиг-секция: `[tool.kb.kbdoc.ingest]` (store/embedding/chunker/collection).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core.chunker_factory import build_chunker
from boba.tool.kb.core.chunker_params import ChunkerParams
from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tool.kb.kbdoc._ingest_common import run_kbdoc_ingest
from boba.tool.kb.kbdoc._workspace_indexing import (
    WorkspaceTransport,
    WorkspaceWalkRequestSource,
)
from boba.tools import FromConfig, FromDI, LLMStringList, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["KbdocIngestConfig", "kbdoc_ingest_paths"]

_PIPELINE_ID: PipelineId = PipelineId("kb.kbdoc_ingest_paths")


class KbdocIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `kbdoc_ingest_paths`.

    Config-секция: `[tool.kb.kbdoc.ingest]`. Operator-controlled поля:
    store/embedding/chunker/collection. LLM выбирает только `paths` и
    `prune_missing` в tool-вызове.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.kbdoc.ingest",
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
        default="kb_kbdoc",
        min_length=1,
        max_length=255,
        description="Target-коллекция в `kb_chunks`.",
    )


@tool
def kbdoc_ingest_paths(
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[KbdocIngestConfig, FromConfig()],
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

    result = run_kbdoc_ingest(
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
