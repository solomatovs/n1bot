"""Обновление kerberos-тикета перед вызовом инструмента.

Тело инструмента получает готовый тикет и продлить его не может, поэтому
обвязка обязана стоять на всех инструментах секции, чей конфиг несёт
keytab-креды. Тест держит связку: обходчик находит креды в конфиге любой
вложенности, а обвязка ложится на тела.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from boba.chainlit.infra.tickets import KerberosTickets
from boba.db.postgres.config import PostgresConfig
from boba.krb import KeytabConfig


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Проверка работает с моделями конфига: сессия приложения ей не нужна."""


class Fixtures:
    """Конфиг соединения с keytab-кредами и инструмент, которому он положен."""

    PRINCIPAL: ClassVar[str] = "boba-svc@LOSHARA.COM"
    CCACHE: ClassVar[str] = "FILE:/tmp/krb5cc_probe"  # noqa: S108

    @classmethod
    def keytab(cls) -> KeytabConfig:
        return KeytabConfig(
            keytab="/etc/boba/boba-svc.keytab",
            principal=cls.PRINCIPAL,
            ccache=cls.CCACHE,
        )

    @classmethod
    def connection(cls) -> PostgresConfig:
        return PostgresConfig.model_validate(
            {
                "host": "pg.loshara.com",
                "user": "boba-svc",
                "dbname": "boba",
                "gssencmode": "require",
                "kerberos": cls.keytab().model_dump(mode="json"),
            }
        )

    @classmethod
    def tool_config(cls) -> BaseModel:
        """Конфиг секции: соединения лежат словарём профилей, как у pg."""

        class ToolConfig(BaseModel):
            profiles: dict[str, PostgresConfig]

        return ToolConfig(profiles={"main": cls.connection()})

    @classmethod
    def tool(cls) -> StructuredTool:
        class Args(BaseModel):
            sql: Annotated[str, "запрос"]

        async def body(sql: str) -> str:
            return sql

        return StructuredTool(
            name="probe_query",
            description="проба",
            args_schema=Args,
            coroutine=body,
        )


class TestTicketsAreBound:
    """Обвязка ставится там, где в конфиге есть keytab, и только там."""

    def test_walker_finds_nested_credentials(self) -> None:
        found = list(KerberosTickets.keytabs_of([Fixtures.tool_config()]))  # noqa: SLF001

        if len(found) != 1:
            raise AssertionError(f"креды не найдены в конфиге секции: {found}")

        if found[0].principal != Fixtures.PRINCIPAL:
            raise AssertionError(f"найдены чужие креды: {found[0]}")

    def test_body_is_wrapped(self) -> None:
        tool = Fixtures.tool()
        before = tool.coroutine

        KerberosTickets.bind_all([tool], [Fixtures.tool_config()])

        if tool.coroutine is before:
            raise AssertionError("обвязка обновления тикета не поставлена")

    def test_config_without_kerberos_is_left_alone(self) -> None:
        tool = Fixtures.tool()
        before = tool.coroutine

        plain: dict[str, Any] = {"max_rows": 100}
        KerberosTickets.bind_all([tool], [plain])

        if tool.coroutine is not before:
            raise AssertionError("обвязка поставлена там, где kerberos не нужен")
