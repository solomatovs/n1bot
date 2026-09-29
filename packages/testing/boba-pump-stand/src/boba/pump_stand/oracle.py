"""Схема Oracle стенда перекачки: пользователь PUMP_STAND, которого
пересоздаёт администратор источника и сносит после тестов (StandOracleUser).
Таблицы в схеме создаёт сам владелец: готовую customers или любые стейтменты
теста.

Ошибки:
OracleQueryError — сервер отклонил DDL стенда.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.payload import PayloadOracle
from boba.pump_stand.stand import OraSource
from boba.stand.names import StandNames
from boba.stand.oracle_user import StandOracleUser

__all__ = ["OracleStand", "PumpUser"]


class PumpUser:
    """Имя схемы стенда перекачки: PUMP_STAND с меткой рабочего процесса —
    по нему тесты обращаются к таблицам владельца."""

    BASE: ClassVar[str] = "PUMP_STAND"

    def __init__(self) -> None:
        self._names = StandNames()

    @property
    def name(self) -> str:
        return self._names.of(self.BASE).upper()


class OracleStand:
    """Пересоздаёт схему PUMP_STAND на источнике: пустую (recreate_user, затем
    стейтменты теста через run) или сразу с таблицей customers (recreate)."""

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
        self._user = StandOracleUser(source.admin, PumpUser.BASE)

    @property
    def owner(self) -> OracleConfig:
        return self._user.owner

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
        await self._user.recreate()

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
        await self._user.drop()
