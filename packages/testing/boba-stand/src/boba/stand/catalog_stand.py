"""Стенд каталога на живом Postgres: хранилища на своих тестовых схемах,
сервис над шиной в памяти, фейковый вид снимка под фейк снятия, площадка
фейка и сборщик событий области пользователя.

Ошибки:
TypeError — сервис стенда поднят не над шиной в памяти, подписаться нельзя.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from psycopg import sql

from boba.catalog import SourceKinds
from boba.catalog_service import (
    CatalogConfig,
    CatalogService,
    ConnectionStore,
    ProcessStore,
    SyncPorts,
)
from boba.db.clickhouse.snapshot import ChSnapshot
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.catalog import CatalogStoreConfig
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot import PgSnapshot
from boba.identity.context import Scope, Subject
from boba.messaging import CatalogChanged, Envelope, MemoryMessageBus
from boba.stand.catalog_ports import FakeSyncSite

__all__ = ["CatalogStand", "ChangeCollector", "FakeKindSnapshot"]


class FakeKindSnapshot(PgSnapshot):
    """Снимок вида postgres, чей инструмент снятия — фейк стенда."""

    SYNC_TOOL = "fake_pg_snapshot"


class CatalogStand:
    """Хранилища каталога на тестовых схемах: build сносит обе схемы и
    поднимает ProcessStore и ConnectionStore заново, service собирает над
    ними CatalogService с шиной в памяти. Тесты сервиса, хранилищ, api и
    инструментов каталога строят его из фикстуры pool."""

    BUS_NAME: ClassVar[str] = "test:0"

    def __init__(
        self,
        cfg: CatalogConfig,
        processes: ProcessStore,
        connections: ConnectionStore,
    ) -> None:
        self.cfg = cfg
        self.processes = processes
        self.connections = connections

    @staticmethod
    def config(
        schema: str, view_roles: Sequence[str], edit_roles: Sequence[str]
    ) -> CatalogConfig:
        """Секция каталога стенда: домен в schema, приложение в schema_app."""
        return CatalogConfig(
            enable=True,
            db_schema=schema,
            app_schema=f"{schema}_app",
            view_roles=tuple(view_roles),
            edit_roles=tuple(edit_roles),
        )

    @staticmethod
    def kinds() -> SourceKinds:
        """Реестр видов: оба снимка из пакетов драйверов."""
        return SourceKinds.of(PgSnapshot, ChSnapshot)

    @staticmethod
    def fake_kinds() -> SourceKinds:
        """Реестр видов с фейковым postgres: синхронизацию гонит фейк снятия."""
        return SourceKinds.of(FakeKindSnapshot, ChSnapshot)

    @classmethod
    async def reset(cls, pool: AsyncPostgresPool, cfg: CatalogConfig) -> None:
        """Обе схемы каталога снесены каскадом."""
        async with pool.connection() as conn:
            for schema in (cfg.db_schema, cfg.app_schema):
                await conn.execute(
                    sql.SQL("drop schema if exists {} cascade").format(
                        sql.Identifier(schema)
                    )
                )

    @classmethod
    async def build(
        cls, pool: AsyncPostgresPool, cfg: CatalogConfig, kinds: SourceKinds
    ) -> CatalogStand:
        await cls.reset(pool, cfg)

        processes = ProcessStore(cfg, pool)
        await processes.setup()

        connections = ConnectionStore(cfg, kinds, pool)
        await connections.setup()

        return cls(cfg, processes, connections)

    def service(self, ports: SyncPorts) -> CatalogService:
        return CatalogService(
            self.processes,
            self.connections,
            self.cfg,
            MemoryMessageBus(self.BUS_NAME),
            ports,
        )

    def fake_site(
        self, workdir: Path, role: str, profile: str, postgres: PostgresConfig
    ) -> FakeSyncSite:
        """Площадка фейка снятия над доменом этого стенда."""
        catalog = CatalogStoreConfig(connection=postgres, db_schema=self.cfg.db_schema)
        return FakeSyncSite(
            workdir=workdir, role=role, profile=profile, catalog=catalog
        )


class ChangeCollector:
    """Подписчик области пользователя на шине в памяти: копит сообщения
    CatalogChanged с момента listen, пока его не отпишут через leave."""

    def __init__(self, bus: MemoryMessageBus, scope: Scope) -> None:
        self.seen: list[CatalogChanged] = []
        self._leave = bus.subscribe(scope, self)

    async def __call__(self, envelope: Envelope) -> None:
        if not isinstance(envelope.message, CatalogChanged):
            return

        self.seen.append(envelope.message)

    @classmethod
    def listen(cls, service: CatalogService, subject: Subject) -> ChangeCollector:
        bus = service.bus
        if not isinstance(bus, MemoryMessageBus):
            got = type(bus).__name__
            msg = (
                f"catalog stand: subscribing {subject.login!r} to catalog changes "
                f"expects the service over a MemoryMessageBus, got {got}"
            )
            raise TypeError(msg)

        return cls(bus, Scope.user(subject.user_id))

    def leave(self) -> None:
        self._leave()
