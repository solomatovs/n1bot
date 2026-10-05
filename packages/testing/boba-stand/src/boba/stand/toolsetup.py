"""Сборка инструментов плагинов из конфига для тестов: зигота секции в
песочнице, конфиги и соединения инструментов, вызов с разбором результата.

Так инструмент собирает процесс, который его исполняет (сервис boba-mcp);
конфиг — его же: секции [tool.<id>] из conf/plugins.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from omegaconf import OmegaConf

from boba.config import bind
from boba.db.postgres.connection import PostgresConfig
from boba.kerberos import KeytabAuth
from boba.krb import KeytabCredentials, ServiceTicketIssuer
from boba.runtime.launchers import ZygoteLaunchers
from boba.sandbox.zygote import ZygotePolicy, ZygoteToolCaller
from boba.stand.sandbox import section_profile
from boba.stand.zygote import ZygoteStand
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebToolsConfig
from boba.toolkit.chain import CallAmbient
from boba.toolkit.launcher import LauncherFactory, ToolLauncher
from boba.toolkit.result import ToolArtifact
from boba.transport.http.connection import HttpConnection

__all__ = ["ZYGOTE", "Call", "ToolSetup"]

USER_ID = "integration"
THREAD_ID = "t-integration"


ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


class ToolSetup:
    """Сборка инструмента из конфига приложения для прогона вне chainlit."""

    @staticmethod
    def config(raw: Any, section: str, model: type) -> Any:
        """Секция конфига как есть, с cgroup-лимитами — они часть контракта."""
        return bind(raw, path=section, model=model)

    @staticmethod
    def path_vars() -> dict[str, str]:
        return {"user_id": USER_ID, "thread_id": THREAD_ID}

    @staticmethod
    def pg_config(raw: Any) -> PgToolConfig:
        """Лимиты выдачи [tool.pg]: соединение приходит параметром вызова."""
        return bind(raw, path="tool.pg", model=PgToolConfig)

    @staticmethod
    def pg_connection(raw: Any) -> PostgresConfig:
        """Профиль соединения теста: сервисный [postgres] с билетом вызова.

        В приложении профиль подаёт обвязка из таблицы соединений, а
        kerberos-секция едет внутрь билетом — ccache сервиса в песочнице нет.
        """
        service = bind(raw, path="postgres", model=PostgresConfig)

        auth = service.auth
        if not isinstance(auth, KeytabAuth):
            return service

        issuer = ServiceTicketIssuer(auth.min_lifetime)
        source = KeytabCredentials.of(auth)
        ticket = issuer.issue(source, service.service_name())

        return service.model_copy(update={"auth": ticket})

    @staticmethod
    def web_config(raw: Any) -> WebToolsConfig:
        """Лимиты выдачи [tool.web]: соединение приходит параметром вызова."""
        return bind(raw, path="tool.web", model=WebToolsConfig)

    @staticmethod
    def web_connection(raw: Any) -> HttpConnection:
        """Профиль соединения теста: в бою его подаёт хост из строк субъекта."""
        return bind(raw, path="tool.ingest.confluence", model=HttpConnection)

    @staticmethod
    def sandbox_raw(raw: Any) -> Any:
        """Зиготы тестов живут в песочнице: режимные развилки конфига — sandbox."""
        copied = raw.copy()
        OmegaConf.update(copied, "env.tool_launcher", "sandbox")
        return copied

    @staticmethod
    def caller(
        zygote_stand: ZygoteStand, raw: Any, section: str, modules: Sequence[str] = ()
    ) -> ZygoteToolCaller:
        """Зигота секции конфига: тот же путь запуска, что в приложении."""
        raw = ToolSetup.sandbox_raw(raw)
        connection = section_profile(raw, section)

        supervisor = zygote_stand.registry().obtain(
            section,
            connection,
            modules,
            ZYGOTE,
            warmup_calls=ZygoteLaunchers.warmup_configs(section, modules, raw),
        )
        return ZygoteToolCaller(
            section, supervisor, connection, CallAmbient(), ToolSetup.path_vars
        )

    @staticmethod
    def launchers(zygote_stand: ZygoteStand, raw: Any, section: str) -> LauncherFactory:
        """Фабрика исполнителей секции: одна зигота на все её инструменты."""
        caller = ToolSetup.caller(zygote_stand, raw, section)

        def launcher(tool: str) -> ToolLauncher:
            return caller

        return launcher

    @staticmethod
    def by_name(built: list[Any]) -> dict[str, Any]:
        tools: dict[str, Any] = {}
        for tool in built:
            tools[tool.name] = tool
        return tools


class Call:
    """Вызов инструмента: ответ разбирается как типизированный artifact."""

    @staticmethod
    async def result(tool: Any, **args: Any) -> Any:
        message = await tool.ainvoke(
            {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
        )
        result = ToolArtifact.revive(message.artifact)
        if result is None:
            raise AssertionError(f"{tool.name}: artifact не разобран")
        return result

    @staticmethod
    async def ok(tool: Any, **args: Any) -> Any:
        result = await Call.result(tool, **args)
        if not (result.ok):
            raise AssertionError(f"{tool.name}: {result}")
        return result
