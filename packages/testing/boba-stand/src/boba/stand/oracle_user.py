"""Пользователь Oracle стенда: схема, которую пересоздаёт администратор
источника и сносит после тестов.

Стенды разных пакетов пишут на общие серверы Oracle, поэтому имя
пользователя несёт метку набора и процесса (StandNames), пароль равен имени в
нижнем регистре. Администратор выдаёт только create session и create table с
квотой на users: таблицы в схеме создаёт сам владелец.

Ошибки:
OracleQueryError — сервер отклонил DDL администратора.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from oracledb import AsyncConnection
from pydantic import SecretStr

from boba.db.oracle import OracleQueryError
from boba.db.oracle.connection import OracleConfig, PasswordAuth
from boba.db.oracle.payload import PayloadOracle
from boba.stand.names import StandNames

__all__ = ["StandOracleUser"]


class StandOracleUser:
    """Схема стенда на сервере Oracle: имя BASE с меткой StandNames,
    пересоздание и снос профилем администратора, профиль владельца для
    соединений стенда. Сессию только что закрытого соединения сервер снимает
    не сразу, поэтому drop user повторяется с паузой на ORA-01940."""

    NO_SUCH_USER: ClassVar[str] = "ORA-01918"
    STILL_CONNECTED: ClassVar[str] = "ORA-01940"
    DROP_ATTEMPTS: ClassVar[int] = 20
    DROP_PAUSE: ClassVar[float] = 0.25

    def __init__(self, admin: OracleConfig, base: str) -> None:
        self._admin = admin
        self._base = base
        self._names = StandNames()

    @property
    def name(self) -> str:
        return self._names.of(self._base).upper()

    def secret(self) -> str:
        return self.name.lower()

    @property
    def owner(self) -> OracleConfig:
        """Профиль владельца: адрес администратора, учётка схемы."""
        auth = PasswordAuth(
            method="password", user=self.name, password=SecretStr(self.secret())
        )
        return self._admin.model_copy(update={"auth": auth})

    async def recreate(self) -> None:
        payload = PayloadOracle(self._admin)
        async with payload.opened() as admin:
            await self._drop(payload, admin)
            await self._run(
                payload,
                admin,
                f"create user {self.name} identified by {self.secret()} "
                "default tablespace users quota unlimited on users",
            )
            await self._run(
                payload, admin, f"grant create session, create table to {self.name}"
            )

    async def drop(self) -> None:
        payload = PayloadOracle(self._admin)
        async with payload.opened() as admin:
            await self._drop(payload, admin)

    async def _drop(self, payload: PayloadOracle, admin: AsyncConnection) -> None:
        statement = f"drop user {self.name} cascade"
        for attempt in range(1, self.DROP_ATTEMPTS + 1):
            try:
                await self._run(payload, admin, statement)
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
