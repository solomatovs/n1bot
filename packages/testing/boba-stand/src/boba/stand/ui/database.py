"""База UI-стенда: создание, расширения, снос таблиц прошлых прогонов и посев соединений
для одного приложения; kerberos как у приложения, один пул на операцию.

Ошибки:
StandError — база не подготовлена (нет прав на расширение и т.п.).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import Any, ClassVar

from psycopg import sql
from psycopg.errors import InsufficientPrivilege

from boba.catalog_service import CatalogConfig, CatalogTable
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.profile import ConnectionTable, GrantTarget, StoredRole
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.db.postgres import AsyncPostgresPool, SqlNames
from boba.db.postgres.profile.config import PostgresConfig
from boba.identity.session import UserMetadataField
from boba.runtime.config import DataLayerConfig
from boba.stand.site import StandLayers
from boba.stand.ui.stand import REPO_ROOT, StandApp, StandConfig, StandError, StandUrl
from boba.transport.http.profile import HttpProfile
from boba.workflow.records import WorkflowTable
from boba.workflow_engine.store import WorkflowConfig


class StandExtension(StrEnum):
    """Расширения базы стенда: без них приложение не поднимается."""

    VECTOR = "vector"
    PG_TRGM = "pg_trgm"
    UNACCENT = "unaccent"
    BTREE_GIN = "btree_gin"

    def statement(self) -> sql.Composed:
        return sql.SQL("create extension if not exists {}").format(
            sql.Identifier(self.value)
        )

    def manual_hint(self, database: str) -> str:
        return (
            f"stand database {database} has no {self.value} extension "
            f"and the application role may not create it; "
            f"run as a superuser: "
            f"psql -d {database} -c 'create extension {self.value}'"
        )


def run_blocking(work: Coroutine[Any, Any, Any]) -> Any:
    """Гонит корутину в своём потоке: у сессии pytest может быть живой event loop."""
    with ThreadPoolExecutor(max_workers=1) as runner:
        return runner.submit(asyncio.run, work).result()


class StandDatabase:
    """База стенда приложения: готовится до старта процесса, сеется после него."""

    POOL_OVERRIDE: ClassVar[dict[str, Any]] = {
        "min_size": 1,
        "max_size": 1,
        "timeout": 30.0,
    }

    def __init__(self, app: StandApp, name: str) -> None:
        self._app = app
        self._name = name
        self._built = StandLayers.compose(app.base_config.under(REPO_ROOT))
        layer = bind(self._built, path=app.data_layer_section, model=DataLayerConfig)
        pool = layer.postgres.pool.model_copy(update=self.POOL_OVERRIDE)
        self._maintenance = layer.postgres.model_copy(update={"pool": pool})
        self._postgres = layer.postgres.model_copy(
            update={"dbname": name, "pool": pool}
        )
        self._schema = layer.db_schema

    @property
    def schema(self) -> str:
        return self._schema

    def prepare(self) -> str:
        """База создана, расширения на месте, таблицы прошлых прогонов снесены."""
        run_blocking(self._prepare())
        return self._name

    async def _prepare(self) -> None:
        await self._ensure_database()
        await self._ensure_extensions()
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        workflow = bind(self._built, path="workflow", model=WorkflowConfig)
        async with self._pool() as pool, pool.cursor() as cur:
            for table in (ConnectionTable.GRANTS, ConnectionTable.CONNECTIONS):
                await cur.execute(self._drop(connections.db_schema, table.value))

            workflow_tables = (
                WorkflowTable.RUNS,
                WorkflowTable.WORKFLOWS,
            )
            for table in workflow_tables:
                await cur.execute(self._drop(workflow.db_schema, table.value))

        catalog = bind(self._built, path="catalog", model=CatalogConfig)
        async with self._pool() as pool, pool.cursor() as cur:
            await cur.execute(
                sql.SQL("drop schema if exists {} cascade").format(
                    sql.Identifier(catalog.db_schema)
                )
            )

        await self._forget_studio_profiles()

    @staticmethod
    def _drop(schema: str, table: str) -> sql.Composed:
        return sql.SQL("drop table if exists {} cascade").format(
            sql.Identifier(schema, table)
        )

    async def _ensure_database(self) -> None:
        maintenance = AsyncPostgresPool(self._maintenance)
        await maintenance.open()
        try:
            async with maintenance.cursor() as cur:
                await cur.execute(
                    "select 1 from pg_database where datname = %s", (self._name,)
                )
                exists = await cur.fetchone()
                if not exists:
                    await cur.execute(
                        sql.SQL("create database {}").format(sql.Identifier(self._name))
                    )
        finally:
            await maintenance.close()

    async def _ensure_extensions(self) -> None:
        async with self._pool() as pool, pool.cursor() as cur:
            for extension in StandExtension:
                await cur.execute(
                    "select 1 from pg_extension where extname = %s", (extension.value,)
                )
                installed = await cur.fetchone()
                if installed:
                    continue

                try:
                    await cur.execute(extension.statement())
                except InsufficientPrivilege as exc:
                    raise StandError(extension.manual_hint(self._name)) from exc

    async def _forget_studio_profiles(self) -> None:
        """Выбор профиля studio хранится на пользователе и пережил бы прогон."""
        query = sql.SQL("update {}.users set meta = meta - %s where meta ? %s").format(
            sql.Identifier(self._schema)
        )
        try:
            async with self._pool() as pool, pool.cursor() as cur:
                await cur.execute(
                    query,
                    (
                        UserMetadataField.STUDIO_PROFILE,
                        UserMetadataField.STUDIO_PROFILE,
                    ),
                )
        except Exception as exc:
            # таблицы users ещё нет у чистой базы: приложение создаст её на старте
            if "does not exist" not in str(exc):
                raise

    def wipe_llm_settings(self) -> None:
        """Снимает сохранённые настройки LLM у всех пользователей базы."""
        query = sql.SQL("update {}.users set meta = meta - 'llm'").format(
            sql.Identifier(self._schema)
        )
        run_blocking(self._execute(query, None))

    def elements_named(self, name: str) -> int:
        """Сколько элементов с таким именем записал data layer стенда."""
        query = sql.SQL("select count(*) from {}.elements where name = %s").format(
            sql.Identifier(self._schema)
        )
        row = run_blocking(self._execute(query, (name,)))
        if row is None:
            return 0

        return int(row[0])

    def catalog_portions(self, draft_id: str) -> int:
        """Сколько порций операций записано в черновик каталога."""
        catalog = bind(self._built, path="catalog", model=CatalogConfig)
        query = sql.SQL("select count(*) from {} where draft_id = %s").format(
            SqlNames.table(catalog.db_schema, CatalogTable.DRAFT_OPS)
        )
        row = run_blocking(self._execute(query, (draft_id,)))
        if row is None:
            return 0

        return int(row[0])

    def llm_settings_of(self, identifier: str) -> dict[str, Any]:
        """Ключ llm из users.meta: тест сверяет, что именно сохранилось."""
        query = sql.SQL(
            "select coalesce(meta -> 'llm', '{{}}'::jsonb) "
            "from {}.users where identifier = %s"
        ).format(sql.Identifier(self._schema))
        row = run_blocking(self._execute(query, (identifier,)))
        if row is None:
            raise RuntimeError(f"user {identifier} is not stored")

        return dict(row[0])

    def break_connection_kind(self, name: str, kind: str) -> None:
        """Строке connections по имени ставится несуществующий kind.

        Стенд пометки «type not installed»: приложение видит строку, чей
        пакет-владелец как будто удалён.
        """
        run_blocking(self._break_connection_kind(name, kind))

    async def _break_connection_kind(self, name: str, kind: str) -> None:
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        query = sql.SQL(
            "update {} "
            "set data = jsonb_set(data, '{{kind}}', to_jsonb(%(kind)s::text)) "
            "where name = %(name)s"
        ).format(SqlNames.table(connections.db_schema, ConnectionTable.CONNECTIONS))

        async with self._pool() as pool, pool.cursor() as cur:
            await cur.execute(query, {"kind": kind, "name": name})

    def seed_connections(self, llm_port: int) -> None:
        """Соединения инструментов стенда: сервисные pg/ch под именем main и web-профиль
        фейкового сервера, выданные всем ролям стенда. Таблица чистится перед посевом;
        роли появляются на старте приложения — сеять после него.
        """
        run_blocking(self._seed_connections(llm_port))

    async def _seed_connections(self, llm_port: int) -> None:
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        clickhouse = bind(self._built, path="clickhouse", model=ClickHouseConfig)
        web = HttpProfile(base_url=StandUrl.of(llm_port), ssl_verify=False)
        async with self._pool() as pool:
            store = ConnectionStore(connections, ConnectionTypes.discover(), pool)
            # строки прошлых прогонов могут не проходить нынешний валидатор
            # профиля, поэтому чистятся мимо стора
            async with pool.cursor() as cur:
                for table in (ConnectionTable.GRANTS, ConnectionTable.CONNECTIONS):
                    await cur.execute(
                        sql.SQL("delete from {}").format(
                            SqlNames.table(connections.db_schema, table)
                        )
                    )

            roles = StoredRole.by_name(await store.roles())
            targets: list[GrantTarget] = []
            for role_names in StandConfig.STAND_ROLES.values():
                for role in role_names:
                    targets.append(GrantTarget.role(roles[role]))

            rows = [
                await store.add("main", self._postgres),
                await store.add("main", clickhouse),
                await store.add("stand", web),
            ]
            for connection_id in rows:
                for target in targets:
                    await store.grant(connection_id, target)

    async def _execute(
        self, query: sql.Composed, params: tuple[Any, ...] | None
    ) -> Any:
        async with self._pool() as pool, pool.cursor() as cur:
            await cur.execute(query, params)
            if cur.description is None:
                return None

            return await cur.fetchone()

    def _pool(self) -> _OpenedPool:
        return _OpenedPool(self._postgres)


class _OpenedPool:
    """Пул на одну операцию: открыт на входе, закрыт на выходе."""

    def __init__(self, postgres: PostgresConfig) -> None:
        self._pool = AsyncPostgresPool(postgres)

    async def __aenter__(self) -> AsyncPostgresPool:
        await self._pool.open()
        return self._pool

    async def __aexit__(self, *error: object) -> None:
        await self._pool.close()
