"""Схема Oracle стенда перекачки: пользователь PUMP_STAND, которого
пересоздаёт администратор источника и сносит после тестов — стенд общий со
скрапером словаря, лишняя схема ломает его эталон. Таблицы в схеме создаёт
сам владелец: готовую customers или любые стейтменты теста.

Ошибки:
OracleQueryError — сервер отклонил DDL стенда.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import ClassVar

from oracledb import AsyncConnection

from boba.db.oracle import OracleQueryError
from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.payload import PayloadOracle
from boba.pump_stand.names import StandNames
from boba.pump_stand.stand import OraSource

__all__ = ["OracleStand", "PumpUser"]


class PumpUser:
    """Схема стенда перекачки: PUMP_STAND с меткой рабочего процесса, пароль
    учётки равен имени в нижнем регистре."""

    BASE: ClassVar[str] = "PUMP_STAND"

    def __init__(self) -> None:
        self._names = StandNames()

    @property
    def name(self) -> str:
        return self._names.of(self.BASE).upper()

    def secret(self) -> str:
        return self.name.lower()


class OracleStand:
    """Пересоздаёт схему PUMP_STAND на источнике: пустую (recreate_user, затем
    стейтменты теста через run) или сразу с таблицей customers (recreate)."""

    NO_SUCH_USER: ClassVar[str] = "ORA-01918"
    STILL_CONNECTED: ClassVar[str] = "ORA-01940"
    DROP_ATTEMPTS: ClassVar[int] = 20
    DROP_PAUSE: ClassVar[float] = 0.25

    OWNER: ClassVar[tuple[str, ...]] = (
        "create table customers ("
        " id number(10) not null,"
        " email varchar2(200) not null,"
        " balance number(18, 2) default 0 not null,"
        " created_at timestamp(6) not null,"
        " note varchar2(200),"
        " photo raw(16),"
        " constraint customers_pk primary key (id))",
    )

    FILL: ClassVar[str] = (
        "insert into customers (id, email, balance, created_at, note, photo)"
        " select level, 'user' || level || '@example.com', level / 4,"
        " timestamp '2024-02-29 13:14:15.123456' + numtodsinterval(level, 'second'),"
        " case when mod(level, 3) = 0 then null else 'note, \"' || level || '\"' end,"
        " case when mod(level, 5) = 0 then null else hextoraw('00ff10') end"
        " from dual connect by level <= :n"
    )

    def __init__(self, source: OraSource) -> None:
        self._source = source
        self._user = PumpUser()

    @property
    def owner(self) -> OracleConfig:
        return self._source.owner(self._user.name, self._user.secret())

    def _admin(self) -> tuple[str, ...]:
        """Пересоздание пользователя стенда: снос, создание, права."""
        name = self._user.name
        return (
            f"drop user {name} cascade",
            f"create user {name} identified by {self._user.secret()} "
            "default tablespace users quota unlimited on users",
            f"grant create session, create table to {name}",
        )

    async def version(self) -> int:
        """Мажорная версия сервера: 12, 18, 21, 23."""
        payload = PayloadOracle(self._source.admin)
        async with (
            payload.opened() as admin,
            payload.rows(admin, "select version from v$instance") as stream,
        ):
            rows = [row async for row in stream.blocks]

        release, *_ = str(rows[0][0]).split(".")

        return int(release)

    async def recreate_user(self) -> None:
        payload = PayloadOracle(self._source.admin)
        async with payload.opened() as admin:
            await self._drop_user(payload, admin)
            _, *rest = self._admin()
            for statement in rest:
                await self._run(payload, admin, statement)

    async def run(
        self, statements: Sequence[str], parameters: Mapping[str, object] | None = None
    ) -> None:
        """Стейтменты владельцем схемы по порядку одной транзакцией."""
        owner = PayloadOracle(self.owner)
        async with owner.opened() as conn:
            for statement in statements:
                async with owner.rows(conn, statement, parameters):
                    pass

            await owner.commit(conn)

    async def recreate(self, rows: int) -> None:
        await self.recreate_user()
        await self.run(self.OWNER)
        await self.run((self.FILL,), {"n": rows})

    async def drop(self) -> None:
        payload = PayloadOracle(self._source.admin)
        async with payload.opened() as admin:
            await self._drop_user(payload, admin)

    async def _drop_user(self, payload: PayloadOracle, admin: AsyncConnection) -> None:
        """Сессию только что закрытого соединения сервер снимает не сразу, и
        drop user отвечает ORA-01940: повторяется с паузой, потом ошибка."""
        drop, *_ = self._admin()
        for attempt in range(1, self.DROP_ATTEMPTS + 1):
            try:
                await self._run(payload, admin, drop)
            except OracleQueryError as exc:
                if self.NO_SUCH_USER in str(exc):
                    return

                if self.STILL_CONNECTED not in str(exc):
                    raise

                if attempt == self.DROP_ATTEMPTS:
                    raise

                await asyncio.sleep(self.DROP_PAUSE)
                continue

            return

    @staticmethod
    async def _run(payload: PayloadOracle, conn: AsyncConnection, text: str) -> None:
        async with payload.rows(conn, text):
            pass
