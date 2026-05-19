"""
Dishka DI-провайдер для ingest-пути chromadb-tool'а.

Провайдер описывает полный граф зависимостей: `KbIngestToolConfig` →
`Embedder[str]`, `PersistentClient`, `MdChunkParser`, `ChromaVectorStore`,
`MdFolderIndexer`. Tool достаёт верхушку графа (`MdFolderIndexer`) и
не знает про слои.

`KbIngestTool.execute()` зовёт `container.get(MdFolderIndexer)` лениво —
при первом execute Dishka рекурсивно собирает граф, кеширует на
`Scope.APP`. Последующие execute — `get` возвращает закешированный
indexer без работы.

PersistentClient проксируется через `get_chroma_client(persist_path)` —
process-singleton по пути, общий с read-side (`kb_search`,
`kb_list_collections`). Иначе SQLite-бэкэнд chromadb словит file-lock
contention между двумя клиентами на один path.

Override: наследник провайдера (или отдельный Provider в том же
Container) может переопределить любой `@provide` — Dishka выбирает
последнего зарегистрированного. Например, заменить
`ChromaEmbeddingFunctionAdapter` на `OpenAIEmbedder` из `boba-openai`
с асимметричными document/query префиксами, или подсунуть mock-store
в тестах.
"""

from __future__ import annotations

from dishka import Provider, Scope, from_context, provide

from boba.indexing.embedder import Embedder
from boba.tool.chromadb.embedder_factory import EmbedderFactory
from boba.tool.chromadb.kb import get_chroma_client
from boba.tool.chromadb.kb_ingest import KbIngestToolConfig
from boba.tool.chromadb.md_chunk import MdChunkParser
from boba.tool.chromadb.md_folder_ingest import MdFolderIndexer
from boba.tool.chromadb.vector_store import ChromaVectorStore
from chromadb.api import ClientAPI

__all__ = ["ChromadbProvider"]


class ChromadbProvider(Provider):
    """Полный граф зависимостей ingest-tool'а на одном `KbIngestToolConfig`."""

    scope = Scope.APP

    cfg = from_context(provides=KbIngestToolConfig, scope=Scope.APP)
    """`KbIngestToolConfig` приходит из контекста контейнера."""

    @provide
    def embedder_factory(self) -> EmbedderFactory:
        """Stateless factory, переопределяется через subclass-провайдер."""
        return EmbedderFactory()

    @provide
    def embedder(
        self,
        factory: EmbedderFactory,
        cfg: KbIngestToolConfig,
    ) -> Embedder[str]:
        """`Embedder[str]` под cfg.embedding_* параметры.

        Может бросить `EmbeddingModelNotConfiguredError` если
        `cfg.embedding_model == ""` — caller (`KbIngestTool.execute`)
        ловит и возвращает ErrorResult.
        """
        return factory.create(
            model=cfg.embedding_model,
            base_url=cfg.embedding_base_url,
            api_key=cfg.embedding_api_key,
        )

    @provide
    def chromadb_client(self, cfg: KbIngestToolConfig) -> ClientAPI:
        """Proxy к process-singleton `get_chroma_client(persist_path)`.

        Read-side tool'ы (`kb_search`, `kb_list_collections`) ходят через
        тот же singleton — Dishka здесь только декларирует зависимость
        в графе, реальный кеш-объект один на весь процесс по persist_path.
        """
        return get_chroma_client(cfg.persist_path)

    @provide
    def md_chunk_parser(self) -> MdChunkParser:
        """Stateless markdown-парсер; держится в `Scope.APP` ради аллокации."""
        return MdChunkParser()

    @provide
    def vector_store(
        self,
        client: ClientAPI,
        embedder: Embedder[str],
    ) -> ChromaVectorStore:
        """ChromaDB-бэкэнд `VectorStore[str]` для upsert/search/delete."""
        return ChromaVectorStore(client=client, embedder=embedder)

    @provide
    def md_folder_indexer(
        self,
        store: ChromaVectorStore,
        parser: MdChunkParser,
    ) -> MdFolderIndexer:
        """Walker папки → парсер → idempotent upsert в коллекцию."""
        return MdFolderIndexer(store=store, parser=parser)
