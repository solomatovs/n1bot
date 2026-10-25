"""Конфиг сервиса обязан разбираться моделями.

Проверяется тот же файл, с которым работает сервис: его секции инструментов
и профили песочницы. Правки структуры (новая секция, обязательное поле, лимит
профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, ClassVar

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.config import bind
from boba.connection_broker.sealed import SealedConnectionParams, ServerConnection
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import SealKeys
from boba.mcp_server.app import McpAppConfig
from boba.runtime.plugins import PluginKit, PluginMeta, ToolLoader
from boba.runtime.refs import ExecRefs
from boba.stand.refs import StandRefs
from boba.stand.sandbox import plugin_launch, section_profile
from boba.stand_core.context import CallStand
from boba.toolrun.hosted import PluginSection
from boba.toolrun.injected import StaticConfig, ToolConfigError

HEAVY_SECTIONS = ("tool.ingest", "tool.kb")
"""Секции с нативным инференсом: их душит квота базового профиля."""

BASE_CPU_PERCENT = 100
"""Квота базового профиля: одного ядра тяжёлым движкам мало."""


class TestConfigStaysValid:
    """Конфиг разбирается моделями сервиса: пропущенное поле — падение."""

    def test_app_section_binds(self, raw_config: DictConfig) -> None:
        bind(raw_config, path="app", model=McpAppConfig)

    def test_every_tool_section_binds_meta(self, raw_config: DictConfig) -> None:
        tools = OmegaConf.select(raw_config, "tool")
        if not (tools):
            raise AssertionError("в конфиге нет ни одной секции [tool.*]")

        for name in tools:
            bind(raw_config, path=f"tool.{name}", model=PluginMeta)


class TestHeavyToolsGetTheirCpu:
    """Нативный инференс под квотой базового профиля идёт в разы дольше:
    движок берёт размер пула из маски ядер, которую ставит запуск по квоте."""

    @pytest.mark.parametrize("section", HEAVY_SECTIONS)
    def test_cpu_quota_is_raised(self, raw_config: DictConfig, section: str) -> None:
        profile = section_profile(raw_config, section.removeprefix("tool."))
        quota = profile.limits.group_cpu_percent

        if quota is None:
            raise AssertionError(
                f"[{section}]: профиль без group_cpu_percent — секция получит "
                "одно ядро базового профиля"
            )

        if quota <= BASE_CPU_PERCENT:
            raise AssertionError(
                f"[{section}]: group_cpu_percent={quota} — "
                "эмбеддингу и OCR одного ядра мало"
            )


class TestConnectionConfigRefusesStart:
    """Конфиг заданных соединений и правил предложения проверяется на старте:
    сервис не поднимается и называет причину (6.8)."""

    WEB: ClassVar[dict[str, Any]] = {
        "kind": "web",
        "description": "public web",
        "roles": ["*"],
        "connection": {"host": "example.org"},
    }

    def test_connection_name_with_a_dot_is_refused(
        self, raw_config: DictConfig
    ) -> None:
        raw = copy.deepcopy(raw_config)
        raw.connections = {"site.ro": self.WEB}

        with pytest.raises(ValidationError, match=r"site\.ro"):
            bind(raw, path="app", model=McpAppConfig)

    @pytest.mark.parametrize(
        ("key", "value"),
        [("offer", ["shared"]), ("client", "nobody")],
        ids=["source", "client"],
    )
    def test_unknown_source_or_client_of_a_rule_is_refused(
        self, raw_config: DictConfig, key: str, value: object
    ) -> None:
        raw = copy.deepcopy(raw_config)
        raw.mcp.endpoints.general.connections[0][key] = value

        with pytest.raises(ValidationError, match=key):
            bind(raw, path="app", model=McpAppConfig)

    def test_unknown_name_in_a_tool_list_is_refused(
        self, raw_config: DictConfig, runtime_stand: StandRefs
    ) -> None:
        raw = copy.deepcopy(raw_config)
        raw.tool.pg.connections = {"pg_query": ["ghost"]}
        loader = self._loader(raw, runtime_stand, runtime_stand.none().execution())

        with pytest.raises(ToolConfigError, match=r"'ghost'.*not among"):
            loader.load()

    def test_connection_of_another_kind_in_a_tool_list_is_refused(
        self, raw_config: DictConfig, runtime_stand: StandRefs, call_stand: CallStand
    ) -> None:
        raw = copy.deepcopy(raw_config)
        raw.tool.pg.connections = {"pg_query": ["site"]}
        params = SealedConnectionParams(
            SealKeys(),
            ConnectionTypes.discover,
            call_stand.contexts,
            {"site": ServerConnection.model_validate(self.WEB)},
            StaticConfig(),
        )
        refs = replace(runtime_stand.none().execution(), connection_params=params)
        loader = self._loader(raw, runtime_stand, refs)

        with pytest.raises(
            ToolConfigError, match="'site' in its list is of kind 'web'"
        ):
            loader.load()

    @staticmethod
    def _loader(raw: DictConfig, stand: StandRefs, refs: ExecRefs) -> ToolLoader:
        launch = plugin_launch(raw, "pg")
        kit = PluginKit(
            launch=launch,
            meta=bind(raw, path="tool.pg", model=PluginMeta),
            section=PluginSection(
                values=launch.section,
                connections=launch.manifest.connections_in_config,
            ),
        )

        return ToolLoader(
            raw,
            {"pg": kit},
            refs,
            stand.launchers(raw),
            stand.granted("general", ["*"]),
        )
