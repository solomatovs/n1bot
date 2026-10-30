"""Сборка инструментов плагинов из конфига для тестов: зигота секции в
песочнице, конфиги и соединения инструментов, вызов с разбором результата.

Так инструмент собирает процесс, который его исполняет (сервис boba-mcp);
конфиг — его же: секции [tool.<id>] из conf/plugins.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from omegaconf import DictConfig

from boba.config import bind
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import CallContexts
from boba.kerberos import KeytabAuth
from boba.krb import KeytabCredentials, ServiceTicketIssuer
from boba.runtime.launchers import ZygoteLaunchers
from boba.sandbox.program import PluginEntry
from boba.sandbox.zygote import ZygotePolicy, ZygoteToolCaller
from boba.stand.overlay import ConfigOverlay
from boba.stand.refs import StandRefs
from boba.stand.sandbox import plugin_launch
from boba.stand.toolstand import ToolStand
from boba.stand.zygote import ZygoteStand
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebToolsConfig
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import ToolLauncher
from boba.toolrun.hosted import PluginSection
from boba.toolrun.injected import ParamSource
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
    """Сборка инструмента из конфига приложения для прогона вне чата aid."""

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
    def sandbox_raw(raw: DictConfig) -> DictConfig:
        """Зиготы тестов живут в песочнице: режимные развилки конфига — sandbox."""
        return ConfigOverlay(raw).merged({"env.tool_launcher": "sandbox"})

    @staticmethod
    def caller(zygote_stand: ZygoteStand, raw: Any, section: str) -> ZygoteToolCaller:
        """Зигота плагина section из конфига: профиль и прогрев — как в
        приложении, тело — объявление плагина рядом с bash-тулом стенда."""
        raw = ToolSetup.sandbox_raw(raw)
        launch = plugin_launch(raw, section)
        profile = ZygoteLaunchers(raw).profile_of(launch)
        refs = [PluginEntry().ref(section)]

        return zygote_stand.caller(
            section, profile, refs, ToolSetup.path_vars, launch.section
        )

    @staticmethod
    def launchers(
        zygote_stand: ZygoteStand, raw: Any, section: str
    ) -> Callable[[str], ToolLauncher]:
        """Фабрика исполнителей секции: одна зигота на все её инструменты."""
        caller = ToolSetup.caller(zygote_stand, raw, section)

        def launcher(tool: str) -> ToolLauncher:
            return caller

        return launcher

    @staticmethod
    def launched(
        payloads: Sequence[PayloadTool],
        launcher: ToolLauncher,
        sources: Sequence[ParamSource],
        section: PluginSection = ToolStand.NO_SECTION,
    ) -> dict[str, Any]:
        """Инструменты секции по именам под обёрткой запуска launcher и
        источниками параметров sources — как их ставит загрузчик."""
        stand = StandRefs(CallContexts()).tool_stand(sources)

        return ToolSetup.by_name(stand.launch(payloads, launcher, section))

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
        return await tool.run("c1", args)

    @staticmethod
    async def ok(tool: Any, **args: Any) -> Any:
        result = await Call.result(tool, **args)
        if not (result.ok):
            raise AssertionError(f"{tool.name}: {result}")
        return result
