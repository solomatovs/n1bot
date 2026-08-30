"""Билет вызова вместо keytab в статическом конфиге инструмента.

Тело инструмента получает готовый билет, keytab остаётся у приложения:
обвязка ServiceTickets обязана стоять на всех инструментах секции, чей
injected-конфиг несёт keytab-секцию любой вложенности, и только на них.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, ClassVar

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, create_model

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.tickets import ServiceTickets
from boba.connections.credentials import ProfileSections
from boba.connections.kerberos import KeytabAuth, TicketAuth
from boba.connections.postgres import PostgresConfig
from boba.stand.site import Stand
from boba.toolkit.facade import Injected
from boba.toolrun.injected import InjectedConfig

STAND = Stand.required()
KEYTAB = Path(STAND.krb_pg_keytab)
KRB5_CONF = Path(STAND.krb_config)

pytestmark = pytest.mark.anyio

live_kdc = pytest.mark.skipif(
    not KEYTAB.is_file() or not KRB5_CONF.is_file(),
    reason="нет keytab/krb5.conf локального AD",
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Проверка работает с моделями конфига: сессия приложения ей не нужна."""


class ToolConfig(BaseModel):
    """Конфиг секции: соединение лежит вложенно, как у kb/ingest."""

    connection: PostgresConfig
    collection: str = "kb"


class Fixtures:
    """Конфиг соединения с keytab-кредами и инструмент, которому он положен."""

    PRINCIPAL: ClassVar[str] = STAND.service_principal

    @classmethod
    def keytab(cls, ccache: Path) -> KeytabAuth:
        return KeytabAuth(
            method="kerberos_keytab",
            principal=cls.PRINCIPAL,
            keytab=str(KEYTAB),
        )

    @classmethod
    def connection(cls, ccache: Path) -> PostgresConfig:
        return PostgresConfig.model_validate(
            {
                "host": STAND.pg_host,
                "dbname": "boba",
                "connect_timeout": 5,
                "auth": cls.keytab(ccache).model_dump(mode="json"),
            }
        )

    @classmethod
    def plain(cls) -> PostgresConfig:
        return PostgresConfig.model_validate(
            {
                "host": "h",
                "dbname": "d",
                "auth": {"method": "trust", "user": "u"},
            }
        )

    @staticmethod
    def tool(config: BaseModel) -> StructuredTool:
        schema = create_model(
            "ProbeArgs",
            sql=(str, ...),
            cfg=(Annotated[type(config), Injected], ...),
        )

        async def body(**kwargs: object) -> dict[str, object]:
            return kwargs

        tool = StructuredTool(
            name="probe_query",
            description="проба",
            args_schema=schema,
            coroutine=body,
        )

        def resolve(name: str, annotation: Any) -> object:
            return config

        def credentials() -> KerberosCredentialSource:
            return KerberosCredentialSource(None, NoRefresh())

        ServiceTickets.bind_all([tool], credentials, resolve)
        InjectedConfig.bind_all([tool], resolve)
        return tool


class TestArmingIsNeededWhereKeytabLives:
    def test_nested_keytab_is_found(self, tmp_path: Path) -> None:
        config = ToolConfig(connection=Fixtures.connection(tmp_path / "cc"))
        if not ProfileSections.needs_arming(config):
            raise AssertionError("keytab во вложенном профиле не найден")

    def test_plain_config_needs_nothing(self) -> None:
        if ProfileSections.needs_arming(ToolConfig(connection=Fixtures.plain())):
            raise AssertionError("обвязка просится там, где kerberos нет")

    def test_ticket_section_needs_nothing(self) -> None:
        ticket = TicketAuth.of_bytes(Fixtures.PRINCIPAL, "postgres@h", b"x", 60)
        armed = Fixtures.plain().model_copy(
            update={"auth": ticket, "connect_timeout": 5}
        )
        if ProfileSections.needs_arming(ToolConfig(connection=armed)):
            raise AssertionError("готовый билет перевыпускать не нужно")


@live_kdc
class TestTicketReplacesKeytab:
    async def test_body_receives_a_ticket(self, tmp_path: Path) -> None:
        config = ToolConfig(connection=Fixtures.connection(tmp_path / "cc"))

        kwargs = await Fixtures.tool(config).ainvoke({"sql": "select 1"})

        shipped = kwargs["cfg"]
        if not isinstance(shipped, ToolConfig):
            raise AssertionError(f"конфиг потерял тип: {type(shipped)}")
        if not isinstance(shipped.connection.auth, TicketAuth):
            raise AssertionError("телу ушёл не билет")
        if shipped.connection.auth.service != STAND.pg_spn:
            raise AssertionError("билет выпущен не к SPN соединения")
        if shipped.collection != "kb":
            raise AssertionError("остальные поля конфига должны остаться")

    async def test_static_config_keeps_its_keytab(self, tmp_path: Path) -> None:
        """Обвязка не портит базовый конфиг: следующий вызов выпустит новый билет."""
        config = ToolConfig(connection=Fixtures.connection(tmp_path / "cc"))
        tool = Fixtures.tool(config)

        await tool.ainvoke({"sql": "select 1"})

        if not isinstance(config.connection.auth, KeytabAuth):
            raise AssertionError("базовый конфиг переписан билетом")


class TestPlainConfigIsLeftAlone:
    async def test_body_receives_the_config_as_is(self) -> None:
        config = ToolConfig(connection=Fixtures.plain())

        kwargs = await Fixtures.tool(config).ainvoke({"sql": "select 1"})

        if kwargs["cfg"] is not config:
            raise AssertionError("обвязка поставлена там, где kerberos не нужен")
