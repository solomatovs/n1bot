"""План создания KB для явной команды migrate.

Ошибки:
KbMigrationError — размерность вектора негодна.
PostgresError — SQL-ресурсы пакета недоступны или не собираются.
"""

from __future__ import annotations

from boba.db.pgvector.config import KnowledgeBaseSchemaConfig
from boba.db.pgvector.migrations import Migrations
from boba.db.postgres.migrate import MigrationPlan, MigrationScripts
from boba.toolkit.manifest import PluginStorage


class KbSchema(PluginStorage):
    """Собирает DDL общего хранилища kb/ingest без подключения к БД.

    Программа плагина передаёт план команде migrate; сервер его не исполняет.
    """

    def __init__(self, cfg: KnowledgeBaseSchemaConfig) -> None:
        self._cfg = cfg
        self._scripts = MigrationScripts(cfg.tables.pg_schema)
        self._migrations = Migrations(cfg.tables)

    def migration(self) -> str:
        scripts = list(
            self._scripts.package("boba.db.pgvector", self._migrations.names())
        )
        index = self._migrations.vector_index(self._cfg.embedding.dim)
        scripts.append(self._scripts.statement("boba.db.pgvector/vector-index", index))
        plan = MigrationPlan(schema=self._cfg.tables.pg_schema, scripts=tuple(scripts))

        return plan.model_dump_json(by_alias=True)
