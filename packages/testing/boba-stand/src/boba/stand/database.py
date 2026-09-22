"""Тестовая база postgres: создаётся через пул приложения, живёт между прогонами."""

from psycopg import sql

from boba.db.postgres import AsyncPostgresPool, PgQueryBuilder
from boba.db.postgres.profile.config import PostgresConfig


class TestDatabase:
    """Одна база на все пакеты; схемы в ней тесты создают и сносят сами."""

    NAME = "boba_test"

    @classmethod
    async def ensure(cls, postgres: PostgresConfig) -> str:
        """Создаёт базу, если её нет, и отдаёт имя."""
        maintenance = AsyncPostgresPool(postgres)
        await maintenance.open()
        try:
            async with maintenance.cursor() as cur:
                await cur.execute(
                    "select 1 from pg_database where datname = %s", (cls.NAME,)
                )
                exists = await cur.fetchone()
                if not exists:
                    query = (
                        PgQueryBuilder()
                        .add("create database {db}", db=sql.Identifier(cls.NAME))
                        .build()
                    )
                    await cur.execute(query.text, query.params)
        finally:
            await maintenance.close()

        return cls.NAME

    @classmethod
    def config_of(cls, postgres: PostgresConfig, name: str) -> PostgresConfig:
        return postgres.model_copy(update={"dbname": name})
