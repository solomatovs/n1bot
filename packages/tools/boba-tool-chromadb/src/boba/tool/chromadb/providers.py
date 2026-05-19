"""DI-провайдеры chromadb-плагина v2.

Граф зависимостей:

    ChromadbPluginConfig (FromConfig)
        │
        ├──> ClientAPI (chromadb.PersistentClient, generator с close)
        │
        ├──> Embedder[str]                       # ingest-side (через factory)
        │
        ├──> ChromaVectorStore (client + Embedder)
        │
        ├──> ChromaKnowledgeBase (client + cfg → читает EF сам)
        │
        ├──> MdChunkParser
        │
        └──> MdFolderIndexer (store + parser)

Все провайдеры `Scope.APP` — синглтоны на lifetime агента. `ClientAPI`
оформлен как generator-provider: на teardown контейнера Dishka зовёт
cleanup. Включение/отключение плагина целиком — забота framework'а
(`AgentBuilder.discover_plugins` читает `[tool.chromadb]` enable);
если плагин не enabled, его модуль не импортируется → providers не
видны → PersistentClient не открывается, ONNX-модель не скачивается.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from boba.indexing.embedder import Embedder
from boba.tool.chromadb.config import ChromadbPluginConfig
from boba.tool.chromadb.embedder_factory import (
    EmbedderFactory,
    build_chromadb_embedding_function,
)
from boba.tool.chromadb.kb import ChromaKnowledgeBase
from boba.tool.chromadb.md_chunk import MdChunkParser
from boba.tool.chromadb.md_folder_ingest import MdFolderIndexer
from boba.tool.chromadb.vector_store import ChromaVectorStore
from boba.tools import FromConfig, FromDI, Scope, provides
from chromadb.api import ClientAPI

__all__ = [
    "provide_chroma_client",
    "provide_embedder",
    "provide_embedder_factory",
    "provide_knowledge_base",
    "provide_md_chunk_parser",
    "provide_md_folder_indexer",
    "provide_vector_store",
]


@provides(scope=Scope.APP)
def provide_chroma_client(
    cfg: Annotated[ChromadbPluginConfig, FromConfig()],
) -> Iterator[ClientAPI]:
    """PersistentClient с teardown — один клиент на агент.

    В v1 был process-global cache по persist_path; в v2 lifecycle =
    lifecycle Container'а. chromadb PersistentClient не имеет публичного
    close — Dishka просто отпускает ссылку при teardown, GC закрывает
    SQLite.
    """
    import chromadb  # noqa: PLC0415

    client = chromadb.PersistentClient(path=cfg.persist_path)
    try:
        yield client
    finally:
        pass


@provides(scope=Scope.APP)
def provide_embedder_factory() -> EmbedderFactory:
    """Stateless factory; легко переопределить subclass-провайдером."""
    return EmbedderFactory()


@provides(scope=Scope.APP)
def provide_embedder(
    factory: Annotated[EmbedderFactory, FromDI(Scope.APP)],
    cfg: Annotated[ChromadbPluginConfig, FromConfig()],
) -> Embedder[str]:
    """`Embedder[str]` для ingest-стороны.

    Может бросить `EmbeddingModelNotConfiguredError`, если в конфиге
    `embedding_model=""` — обрабатывается в kb_ingest как ErrorResult.
    """
    return factory.create(
        model=cfg.embedding_model,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )


@provides(scope=Scope.APP)
def provide_knowledge_base(
    client: Annotated[ClientAPI, FromDI(Scope.APP)],
    cfg: Annotated[ChromadbPluginConfig, FromConfig()],
) -> ChromaKnowledgeBase:
    """Read-side KB: list_collections + search.

    EF для read-side строится здесь же из cfg (через
    `build_chromadb_embedding_function`) — `None` означает «chromadb
    использует built-in ONNX DefaultEmbeddingFunction».
    """
    ef = build_chromadb_embedding_function(
        model=cfg.embedding_model,
        base_url=cfg.embedding_base_url,
        api_key=cfg.embedding_api_key,
    )
    return ChromaKnowledgeBase(
        client=client,
        snippet_chars=cfg.snippet_chars,
        embedding_function=ef,
    )


@provides(scope=Scope.APP)
def provide_md_chunk_parser() -> MdChunkParser:
    """Stateless markdown-парсер."""
    return MdChunkParser()


@provides(scope=Scope.APP)
def provide_vector_store(
    client: Annotated[ClientAPI, FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
) -> ChromaVectorStore:
    """ChromaDB-бэкэнд VectorStore[str] для upsert/search/delete."""
    return ChromaVectorStore(client=client, embedder=embedder)


@provides(scope=Scope.APP)
def provide_md_folder_indexer(
    store: Annotated[ChromaVectorStore, FromDI(Scope.APP)],
    parser: Annotated[MdChunkParser, FromDI(Scope.APP)],
) -> MdFolderIndexer:
    """Walker папки → парсер → idempotent upsert в коллекцию."""
    return MdFolderIndexer(store=store, parser=parser)
