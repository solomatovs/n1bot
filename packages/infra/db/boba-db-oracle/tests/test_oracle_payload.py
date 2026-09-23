"""Живое подключение к Oracle стенда: имена колонок и строки потоком, именованный
bind, LOB строкой, ошибка запроса и отказ при входе — типами пакета.

Адреса и учётки приходят из секции [ix_stand].ora_sources стендового конфига.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.db.oracle import OracleError, OracleQueryError
from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.payload import PayloadOracle
from boba.stand.ix import IxStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class OraStandSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    oracle: OracleConfig


class OraStand(IxStand):
    ora_sources: Sequence[OraStandSource]


STAND = OraStand.required()


class TestPayloadOracle:
    @pytest.mark.parametrize("name", [source.name for source in STAND.ora_sources])
    async def test_rows_names_lists_and_lobs(self, name: str) -> None:
        source = next(s for s in STAND.ora_sources if s.name == name)
        payload = PayloadOracle(source.oracle)

        async with payload.opened() as conn:
            async with payload.rows(
                conn,
                "select user as who, sys_context('userenv', 'con_name') as con "
                "from dual",
            ) as stream:
                assert stream.names == ("who", "con")
                rows = [row async for row in stream.blocks]
            con_name, _, _ = source.oracle.service.partition(".")
            assert rows == [(source.oracle.auth.user.upper(), con_name.upper())]

            async with payload.rows(
                conn,
                "select u.name from sys.user$ u where u.name = :who",
                {"who": source.oracle.auth.user.upper()},
            ) as stream:
                rows = [row async for row in stream.blocks]
            assert rows == [(source.oracle.auth.user.upper(),)]

            async with payload.rows(
                conn, "select to_clob('lob text') as body from dual"
            ) as stream:
                rows = [row async for row in stream.blocks]
            assert rows == [("lob text",)]

            with pytest.raises(OracleQueryError, match="ORA-00942"):
                async with payload.rows(conn, "select * from no_such_table"):
                    pass

    async def test_wrong_password_is_oracle_error(self) -> None:
        source = STAND.ora_sources[0]
        auth = source.oracle.auth.model_copy(update={"password": SecretStr("wrong")})
        profile = source.oracle.model_copy(update={"auth": auth})

        with pytest.raises(OracleError, match="ORA-01017"):
            async with PayloadOracle(profile).opened():
                pass
