"""Тестовая база postgres: создаётся через пул приложения, живёт между прогонами.
Имя несёт метку разработчика, набора, процесса и дерева (StandNames,
aid_ss_test_aid_gw0_pint4): наборы и процессы xdist пишут в схему приложения
каждый в своей базе. Базы создаются из шаблона с расширениями
(server/template.sql пакета): ставить их может только суперпользователь.
PostgresSessions рвёт на сервере сеансы тестовой базы с одной подписью."""

from typing import ClassVar

from psycopg import sql

from boba.db.postgres import AsyncPostgresPool, PgQueryBuilder
from boba.db.postgres.connection.config import PostgresConfig
from boba.stand.names import StandNames


class TestDatabase:
    """База набора тестов; схемы в ней тесты создают и сносят сами."""

    NAME = "test"
    TEMPLATE = "boba_stand_template"

    @classmethod
    async def ensure(cls, postgres: PostgresConfig) -> str:
        """Создаёт базу, если её нет, и отдаёт имя."""
        name = StandNames().tree_database(cls.NAME)

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


class PostgresSessions:
    """Сеансы тестовой базы под одной подписью application_name.

    Тест обрыва связи подписывает профиль пула, шины или истории своей
    подписью (signed) и рвёт все её сеансы на сервере pg_terminate_backend,
    как перезапуск postgres рвёт соединения процесса; следующая операция
    компонента проверяет, что он восстановился сам. Подключается отдельным
    соединением профиля базы."""

    WAIT_MS: ClassVar[int] = 5000
    """Сколько pg_terminate_backend ждёт конца каждого сеанса."""

    def __init__(self, postgres: PostgresConfig) -> None:
        self._postgres = postgres

    def signed(self, application_name: str) -> PostgresConfig:
        """Профиль базы с подписью сеансов."""
        return self._postgres.model_copy(update={"application_name": application_name})

    async def terminate(self, application_name: str) -> int:
        """Рвёт сеансы базы с подписью и ждёт их конца; сколько оборвано."""
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    pg_terminate_backend(pid, %(wait)s)
                from
                    pg_stat_activity
                where
                    datname = current_database()
                    and application_name = %(app)s
                    and pid <> pg_backend_pid()
                """,
                wait=self.WAIT_MS,
                app=application_name,
            )
            .build()
        )
        async with await AsyncPostgresPool.dedicated(self._postgres) as conn:
            cur = await conn.execute(query.text, query.params)
            rows = await cur.fetchall()

        return len(rows)
