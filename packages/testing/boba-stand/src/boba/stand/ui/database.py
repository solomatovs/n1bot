"""База UI-стенда: создание, расширения, снос таблиц прошлых прогонов и посев соединений
для одного приложения; kerberos как у приложения, один пул на операцию.

Ошибки:
StandError — база не подготовлена (нет прав на расширение и т.п.).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from typing import Any, ClassVar, LiteralString
from uuid import UUID

from omegaconf import OmegaConf
from psycopg import sql
from psycopg.errors import InsufficientPrivilege

from boba.catalog_service import CatalogConfig, CatalogTable
from boba.catalog_service import ConnectionTable as SnapshotTable
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import (
    ConnectionBase,
    ConnectionTable,
    GrantTarget,
    StoredRole,
)
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.snapshot import ChSourceKind
from boba.db.postgres import AsyncPostgresPool, PgQuery, PgQueryBuilder
from boba.db.postgres.connection.config import PostgresConfig
from boba.identity.session import UserMetadataField
from boba.ix_core.database import IxDatabase
from boba.runtime.config import DataLayerConfig
from boba.stand.database import TestDatabase
from boba.stand.edm import EdmDataset, EdmSource, EdmSources
from boba.stand.ix_index import IxPage, IxPages, SharedIndexers, StandIxStack
from boba.stand.site import StandLayers
from boba.stand.stream_sinks import SinkSources, StreamSinks
from boba.stand.ui.stand import REPO_ROOT, StandApp, StandConfig, StandError, StandUrl
from boba.transport.http.connection import HttpConnection, UrlScheme
from boba.workflow.records import WorkflowTable
from boba.workflow_engine.store import WorkflowConfig


class StandExtension(StrEnum):
    """Расширения базы стенда: без них приложение не поднимается."""

    VECTOR = "vector"
    PG_TRGM = "pg_trgm"
    UNACCENT = "unaccent"
    BTREE_GIN = "btree_gin"

    def statement(self) -> PgQuery:
        return (
            PgQueryBuilder()
            .add("create extension if not exists {ext}", ext=sql.Identifier(self.value))
            .build()
        )

    def manual_hint(self, database: str) -> str:
        return (
            f"stand database {database} has no {self.value} extension "
            f"and the application role may not create it; "
            f"stand databases are created from template {TestDatabase.TEMPLATE}: "
            f"run boba-stand server/template.sql as a superuser, or "
            f"psql -d {database} -c 'create extension {self.value}'"
        )


def run_blocking(work: Coroutine[Any, Any, Any]) -> Any:
    """Гонит корутину в своём потоке: у сессии pytest может быть живой event loop."""
    with ThreadPoolExecutor(max_workers=1) as runner:
        return runner.submit(asyncio.run, work).result()


class StandOracle:
    """Oracle стенда под именем main: первый источник [ix_stand].ora_sources,
    учёткой, под которой ходят скраперы. Модель соединения выбирает реестр
    типов по kind, поэтому пакет стенда не зависит от драйвера Oracle."""

    SOURCES: ClassVar[str] = "ix_stand.ora_sources"
    KIND: ClassVar[str] = "oracle"

    def __init__(self, built: Any) -> None:
        sources = OmegaConf.select(built, self.SOURCES)
        if not sources:
            msg = (
                f"stand config: expected at least one oracle source in "
                f"[{self.SOURCES}], got {sources!r}"
            )
            raise StandError(msg)

        raw = OmegaConf.to_container(sources[0].oracle, resolve=True)
        if not isinstance(raw, dict):
            msg = (
                f"stand config: [{self.SOURCES}][0].oracle expects a table, "
                f"got {type(raw).__name__}"
            )
            raise StandError(msg)

        fields: dict[str, Any] = {}
        for key, value in raw.items():
            fields[str(key)] = value

        fields["kind"] = self.KIND
        self._raw = fields

    def connection(self) -> ConnectionBase:
        return ConnectionTypes.discover().parse(self._raw)

    @property
    def host(self) -> str:
        return str(self._raw["host"])

    @property
    def port(self) -> int:
        return int(self._raw["port"])

    @property
    def service(self) -> str:
        return str(self._raw["service"])


class StandDatabase:
    """База стенда приложения: готовится до старта процесса, сеется после него."""

    KB_SCHEMA: ClassVar[str] = "tool.kb.db_schema"
    EMBEDDING_CACHE: ClassVar[str] = "ix_stand.embedding_cache_dir"
    EDM_DATABASE: ClassVar[str] = "edm_ui_stand"
    SINK_CH: ClassVar[str] = "sink_ch"
    SINK_ORA: ClassVar[str] = "sink_ora"
    """База выгрузки ЕДМ UI-стенда: у тестов инструментов своя, прогоны не
    сносят друг другу данные."""

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
                query = (
                    PgQueryBuilder()
                    .add(
                        "drop table if exists {table} cascade",
                        table=sql.Identifier(connections.db_schema, table.value),
                    )
                    .build()
                )
                await cur.execute(query.text, query.params)

            workflow_tables = (
                WorkflowTable.RUNS,
                WorkflowTable.WORKFLOWS,
            )
            for table in workflow_tables:
                query = (
                    PgQueryBuilder()
                    .add(
                        "drop table if exists {table} cascade",
                        table=sql.Identifier(workflow.db_schema, table.value),
                    )
                    .build()
                )
                await cur.execute(query.text, query.params)

        # каталог живёт в studio: домен сносится схемой, таблицы приложения —
        # поимённо, схема приложения общая с users и workflow; у chainlit секция
        # [catalog] — лишь подключение инструмента снятия к домену
        if self._app is StandApp.STUDIO:
            catalog = bind(self._built, path="catalog", model=CatalogConfig)
            async with self._pool() as pool, pool.cursor() as cur:
                query = (
                    PgQueryBuilder()
                    .add(
                        "drop schema if exists {schema} cascade",
                        schema=sql.Identifier(catalog.db_schema),
                    )
                    .build()
                )
                await cur.execute(query.text, query.params)
                for table in CatalogTable:
                    query = (
                        PgQueryBuilder()
                        .add(
                            "drop table if exists {table} cascade",
                            table=sql.Identifier(catalog.app_schema, table.value),
                        )
                        .build()
                    )
                    await cur.execute(query.text, query.params)

                for snapshot_table in SnapshotTable:
                    query = (
                        PgQueryBuilder()
                        .add(
                            "drop table if exists {table} cascade",
                            table=sql.Identifier(
                                catalog.app_schema, snapshot_table.value
                            ),
                        )
                        .build()
                    )
                    await cur.execute(query.text, query.params)

        await self._forget_studio_profiles()

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
                    query = (
                        PgQueryBuilder()
                        .add(
                            "create database {db} template {template}",
                            db=sql.Identifier(self._name),
                            template=sql.Identifier(TestDatabase.TEMPLATE),
                        )
                        .build()
                    )
                    await cur.execute(query.text, query.params)
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
                    query = extension.statement()
                    await cur.execute(query.text, query.params)
                except InsufficientPrivilege as exc:
                    msg = f"{extension.manual_hint(self._name)}: {exc}"
                    raise StandError(msg) from exc

    async def _forget_studio_profiles(self) -> None:
        """Выбор профиля studio хранится на пользователе и пережил бы прогон."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                "update {schema}.users set meta = meta - %(key)s where meta ? %(key)s",
                key=UserMetadataField.STUDIO_PROFILE,
            )
            .build()
        )
        try:
            async with self._pool() as pool, pool.cursor() as cur:
                await cur.execute(query.text, query.params)
        except Exception as exc:
            # таблицы users ещё нет у чистой базы: приложение создаст её на старте
            if "does not exist" not in str(exc):
                raise

    def wipe_llm_settings(self) -> None:
        """Снимает сохранённые настройки LLM у всех пользователей базы."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add("update {schema}.users set meta = meta - 'llm'")
            .build()
        )
        run_blocking(self._execute(query))

    def elements_named(self, name: str) -> int:
        """Сколько элементов с таким именем записал data layer стенда."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                "select count(*) from {schema}.elements where name = %(name)s",
                name=name,
            )
            .build()
        )
        row = run_blocking(self._execute(query))
        if row is None:
            return 0

        return int(row[0])

    def catalog_portions(self, draft_id: str) -> int:
        """Сколько порций операций записано в черновик каталога."""
        catalog = bind(self._built, path="catalog", model=CatalogConfig)
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    count(*)
                from
                    {table}
                where
                    draft_id = %(draft_id)s
                """,
                table=sql.Identifier(catalog.app_schema, CatalogTable.DRAFT_OPS.value),
                draft_id=draft_id,
            )
            .build()
        )
        row = run_blocking(self._execute(query))
        if row is None:
            return 0

        return int(row[0])

    def thread_profile(self, thread_id: str) -> str:
        """Профиль из meta треда; пусто — chainlit ещё не сохранил сессию треда."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                select
                   coalesce(meta ->> 'chat_profile', '')
                from
                    {schema}.threads
                where
                    id = %(id)s
                """,
                id=UUID(thread_id),
            )
            .build()
        )
        row = run_blocking(self._execute(query))
        if row is None:
            msg = f"thread {thread_id!r} has no row in {self._schema}.threads"
            raise RuntimeError(msg)

        return str(row[0])

    def set_thread_profile(self, thread_id: str, name: str) -> None:
        """Профиль в meta треда заменяется на name: так тред выглядит после
        того, как профиль переименовали или убрали из конфига."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                update {schema}.threads
                    set meta = coalesce(meta, jsonb_build_object())
                        || jsonb_build_object('chat_profile', %(name)s::text)
                where
                    id = %(id)s
                """,
                name=name,
                id=UUID(thread_id),
            )
            .build()
        )
        run_blocking(self._execute(query))

    def llm_settings_of(self, identifier: str) -> dict[str, Any]:
        """Ключ llm из users.meta: тест сверяет, что именно сохранилось."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                select
                    coalesce(meta -> 'llm', '{{}}'::jsonb)
                from
                    {schema}.users
                where
                    identifier = %(identifier)s
                """,
                identifier=identifier,
            )
            .build()
        )
        row = run_blocking(self._execute(query))
        if row is None:
            msg = f"user {identifier!r} has no row in {self._schema}.users"
            raise RuntimeError(msg)

        return dict(row[0])

    def break_connection_kind(self, name: str, kind: str) -> None:
        """Строке connections по имени ставится несуществующий kind.

        Стенд пометки «type not installed»: приложение видит строку, чей
        пакет-владелец как будто удалён.
        """
        run_blocking(self._break_connection_kind(name, kind))

    async def _break_connection_kind(self, name: str, kind: str) -> None:
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        query = (
            PgQueryBuilder()
            .add(
                "update {table} "
                "set data = jsonb_set(data, '{{kind}}', to_jsonb(%(kind)s::text)) "
                "where name = %(name)s",
                table=sql.Identifier(
                    connections.db_schema, ConnectionTable.CONNECTIONS.value
                ),
                kind=kind,
                name=name,
            )
            .build()
        )

        async with self._pool() as pool, pool.cursor() as cur:
            await cur.execute(query.text, query.params)

    def ddl(self, statement: LiteralString) -> None:
        """DDL в базе стенда: таблицы-пробники для синхронизации каталога."""
        run_blocking(self._execute(PgQueryBuilder().add(statement).build()))

    def add_connection(self, name: str, kind: str) -> UUID:
        """Подключение стенда к своей базе под именем name: копия профиля
        postgres или clickhouse стенда, выдана всем ролям стенда. Сеятели
        каталога заводят по подключению на источник."""
        return run_blocking(self._add_connection(name, kind))

    async def _add_connection(self, name: str, kind: str) -> UUID:
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        connection: ConnectionBase = self._postgres
        if kind == ChSourceKind.CLICKHOUSE:
            connection = bind(self._built, path="clickhouse", model=ClickHouseConfig)

        async with self._pool() as pool:
            store = ConnectionStore(connections, ConnectionTypes.discover(), pool)
            connection_id = await store.add(name, connection)
            await self._grant_stand_roles(store, (connection_id,))

        return connection_id

    @staticmethod
    async def _grant_stand_roles(
        store: ConnectionStore, connection_ids: Sequence[UUID]
    ) -> None:
        """Выдаёт подключения всем ролям стенда."""
        roles = StoredRole.by_name(await store.roles())
        targets: list[GrantTarget] = []
        for role_names in StandConfig.STAND_ROLES.values():
            for role in role_names:
                targets.append(GrantTarget.role(roles[role]))

        for connection_id in connection_ids:
            for target in targets:
                await store.grant(connection_id, target)

    def remove_connections(self, prefix: str) -> None:
        """Подключения стенда по префиксу имени: снос за сеятелями каталога."""
        run_blocking(self._remove_connections(prefix))

    async def _remove_connections(self, prefix: str) -> None:
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        async with self._pool() as pool:
            store = ConnectionStore(connections, ConnectionTypes.discover(), pool)
            for row in await store.list_all():
                if row.name.startswith(prefix):
                    await store.remove(row.id)

    def seed_connections(self, llm_port: int) -> None:
        """Соединения инструментов стенда: сервисные pg/ch под именем main и
        web-профиль фейкового сервера, выданные всем ролям стенда. Таблица
        чистится перед посевом; роли появляются на старте приложения — сеять
        после него.
        """
        run_blocking(self._seed_connections(llm_port))

    async def _seed_connections(self, llm_port: int) -> None:
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        clickhouse = bind(self._built, path="clickhouse", model=ClickHouseConfig)
        web = HttpConnection(
            scheme=UrlScheme(StandUrl.SCHEME.value),
            host=StandUrl.HOST.value,
            port=llm_port,
            ssl_verify=False,
        )
        async with self._pool() as pool:
            store = ConnectionStore(connections, ConnectionTypes.discover(), pool)
            # строки прошлых прогонов могут не проходить нынешний валидатор
            # профиля, поэтому чистятся мимо стора
            async with pool.cursor() as cur:
                for table in (ConnectionTable.GRANTS, ConnectionTable.CONNECTIONS):
                    query = (
                        PgQueryBuilder()
                        .add(
                            "delete from {table}",
                            table=sql.Identifier(connections.db_schema, table.value),
                        )
                        .build()
                    )
                    await cur.execute(query.text, query.params)

            rows = [
                await store.add("main", self._postgres),
                await store.add("main", clickhouse),
                await store.add("stand", web),
            ]
            await self._grant_stand_roles(store, rows)

    def seed_oracle(self) -> None:
        """Oracle стенда под именем main, выданный ролям стенда: первый источник
        [ix_stand].ora_sources. Сеять после seed_connections: тот чистит таблицу
        соединений."""
        run_blocking(self._seed_oracle())

    async def _seed_oracle(self) -> None:
        oracle = StandOracle(self._built).connection()

        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        async with self._pool() as pool:
            store = ConnectionStore(connections, ConnectionTypes.discover(), pool)
            row = await store.add("main", oracle)
            await self._grant_stand_roles(store, [row])

    def seed_ix(self, pages: Sequence[IxPage]) -> list[int]:
        """Схема ix стека Confluence в базе стенда, страницы pages и индексы
        trgm/fts/vector по ним: поиск kb_*2 идёт по тем же таблицам, что в
        бою. Схема пересоздаётся; результат — id node страниц по порядку."""
        return run_blocking(self._seed_ix(pages))

    async def _seed_ix(self, pages: Sequence[IxPage]) -> list[int]:
        schema = self._setting(self.KB_SCHEMA)
        cache_dir = self._setting(self.EMBEDDING_CACHE)
        database = IxDatabase(db_schema=schema, postgres=self._postgres)

        identifier = sql.Identifier(schema)
        query = (
            PgQueryBuilder()
            .add("drop schema if exists {schema} cascade", schema=identifier)
            .build()
        )
        await self._execute(query)

        await StandIxStack().apply(database)
        nodes = await IxPages(database).write(pages)

        indexers = SharedIndexers(database, cache_dir)
        await indexers.text()
        await indexers.vectors()

        return nodes

    def _setting(self, path: str) -> str:
        value = OmegaConf.select(self._built, path)
        if not value:
            msg = f"stand config: expected a non-empty {path}, got {value!r}"
            raise StandError(msg)

        return str(value)

    def seed_edm(self) -> EdmSource:
        """Выгрузка ЕДМ на первом demo-ClickHouse [ix_stand] и соединение edm к
        ней, выданное ролям стенда; сносит её drop_edm. Сеять после
        seed_connections: тот чистит таблицу соединений."""
        return run_blocking(self._seed_edm())

    async def _seed_edm(self) -> EdmSource:
        source = self._edm_source()
        await EdmDataset(source, self.EDM_DATABASE).recreate()

        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        async with self._pool() as pool:
            store = ConnectionStore(connections, ConnectionTypes.discover(), pool)
            row = await store.add("edm", source.clickhouse)
            await self._grant_stand_roles(store, [row])

        return source

    def drop_edm(self) -> None:
        run_blocking(EdmDataset(self._edm_source(), self.EDM_DATABASE).drop())

    def seed_stream_sinks(self) -> StreamSinks:
        """База ClickHouse и схема Oracle для приёмников насосов и соединения
        sink_ch и sink_ora к ним, выданные ролям стенда; сносит их
        drop_stream_sinks. Сеять после seed_connections: тот чистит таблицу
        соединений."""
        return run_blocking(self._seed_stream_sinks())

    async def _seed_stream_sinks(self) -> StreamSinks:
        sinks = self.stream_sinks()
        await sinks.recreate()

        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        async with self._pool() as pool:
            store = ConnectionStore(connections, ConnectionTypes.discover(), pool)
            rows = [
                await store.add(self.SINK_CH, sinks.ch_connection),
                await store.add(self.SINK_ORA, sinks.ora_connection),
            ]
            await self._grant_stand_roles(store, rows)

        return sinks

    def drop_stream_sinks(self) -> None:
        run_blocking(self.stream_sinks().drop())

    def rows(self, table: str, columns: Sequence[str]) -> list[tuple[Any, ...]]:
        """Строки таблицы public базы стенда по порядку первой из columns."""
        return run_blocking(self._rows(table, columns))

    async def _rows(self, table: str, columns: Sequence[str]) -> list[tuple[Any, ...]]:
        names: list[sql.Identifier] = []
        for column in columns:
            names.append(sql.Identifier(column))

        query = (
            PgQueryBuilder()
            .add(
                "select {columns} from public.{table} order by {order}",
                columns=sql.SQL(", ").join(names),
                table=sql.Identifier(table),
                order=names[0],
            )
            .build()
        )

        rows: list[tuple[Any, ...]] = []
        async with self._pool() as pool, pool.cursor() as cur:
            await cur.execute(query.text, query.params)
            for row in await cur.fetchall():
                rows.append(tuple(row))

        return rows

    def sink_ch_rows(self, table: str, columns: Sequence[str]) -> list[tuple[Any, ...]]:
        return run_blocking(self.stream_sinks().ch_rows(table, columns))

    def sink_ora_rows(
        self, table: str, columns: Sequence[str]
    ) -> list[tuple[Any, ...]]:
        return run_blocking(self.stream_sinks().ora_rows(table, columns))

    def stream_sinks(self) -> StreamSinks:
        """Приёмники насосов стенда: имена базы и схемы, профили и чтение строк."""
        return StreamSinks(bind(self._built, path="ix_stand", model=SinkSources))

    def _edm_source(self) -> EdmSource:
        sources = bind(self._built, path="ix_stand", model=EdmSources).demo()
        if not sources:
            msg = (
                "stand config: expected a demo = true source in "
                "[ix_stand].ch_sources for the edm dataset, got none"
            )
            raise StandError(msg)

        return sources[0]

    async def _execute(self, query: PgQuery) -> Any:
        async with self._pool() as pool, pool.cursor() as cur:
            await cur.execute(query.text, query.params)
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
