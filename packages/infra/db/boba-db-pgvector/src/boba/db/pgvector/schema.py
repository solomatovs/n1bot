"""Параметры SQL-версии общего хранилища KB и ingest.

Ошибки:
PostgresError — каталог выбранной версии не читается.
"""

from boba.db.pgvector.config import KnowledgeBaseSchemaConfig
from boba.db.postgres.migrate import MigrationScripts
from boba.toolkit.manifest import PluginStorage


class KbSchema(PluginStorage):
    """Передаёт общему запускателю SQL пакета и имена из конфигурации хранилища."""

    def __init__(self, cfg: KnowledgeBaseSchemaConfig) -> None:
        self._cfg = cfg
        self._scripts = MigrationScripts(cfg.tables.pg_schema)

    def migration(self, version: str | None = None) -> str:
        tables = self._cfg.tables
        plan = self._scripts.application(
            "boba.db.pgvector",
            version=version,
            values={
                "embedding_dim": self._cfg.embedding.dim,
                "chunks_name": tables.chunks_table,
                "collections_name": tables.collections_table,
                "sources_name": tables.sources_table,
            },
            identifiers={
                "chunks_table": ("pg_temp", tables.chunks_table),
                "collections_table": ("pg_temp", tables.collections_table),
                "sources_table": ("pg_temp", tables.sources_table),
                "chunks_search_index": (f"{tables.chunks_table}_collection_tsv_gin",),
                "chunks_source_index": (
                    f"{tables.chunks_table}_collection_source_chunk",
                ),
                "chunks_vector_index": (f"{tables.chunks_table}_embedding_hnsw",),
                "sources_seen_index": (f"{tables.sources_table}_collection_seen",),
                "sources_parent_index": (f"{tables.sources_table}_collection_parent",),
                "sources_scope_index": (f"{tables.sources_table}_collection_scope",),
                "sources_run_index": (f"{tables.sources_table}_collection_parent_run",),
            },
        )
        return plan.model_dump_json(by_alias=True)
