"""Тестовая база postgres: создаётся через пул приложения, живёт между прогонами.
Имя несёт метку набора и процесса (StandNames): наборы и процессы xdist пишут
в схему приложения каждый в своей базе. Базы создаются из шаблона с расширениями
(server/template.sql пакета): ставить их может только суперпользователь."""

from psycopg import sql

from boba.db.postgres import AsyncPostgresPool, PgQueryBuilder
from boba.db.postgres.connection.config import PostgresConfig
from boba.stand.names import StandNames


class TestDatabase:
    """База набора тестов; схемы в ней тесты создают и сносят сами."""

    NAME = "boba_test"
    TEMPLATE = "boba_stand_template"

    @classmethod
    async def ensure(cls, postgres: PostgresConfig) -> str:
        """Создаёт базу, если её нет, и отдаёт имя."""
        name = StandNames().of(cls.NAME)

        maintenance = AsyncPostgresPool(postgres)
        await maintenance.open()
        try:
            async with maintenance.cursor() as cur:
                await cur.execute(
                    "select 1 from pg_database where datname = %s", (name,)
                )
                exists = await cur.fetchone()
                if not exists:
                    query = (
                        PgQueryBuilder()
                        .add(
                            "create database {db} template {template}",
                            db=sql.Identifier(name),
                            template=sql.Identifier(cls.TEMPLATE),
                        )
                        .build()
                    )
                    await cur.execute(query.text, query.params)
        finally:
            await maintenance.close()

        return name

    @classmethod
    def config_of(cls, postgres: PostgresConfig, name: str) -> PostgresConfig:
        return postgres.model_copy(update={"dbname": name})
