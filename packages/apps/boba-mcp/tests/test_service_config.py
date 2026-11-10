"""Конфиг сервиса обязан разбираться моделями.

Проверяется тот же файл, с которым работает сервис: его секции инструментов
и профили песочницы. Правки структуры (новая секция, обязательное поле, лимит
профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.config import ConfigError, bind
from boba.mcp_server.catalog import McpEndpoint
from boba.mcp_server.config.models import DataConfig, McpAppConfig
from boba.stand.overlay import ConfigOverlay
from boba.stand.sandbox import section_profile
from boba.toolrun.plugins import PluginMeta

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


class TestDatabaseEndpointCatalog:
    """Опубликованные адреса и grants MCP читаются только из БД."""

    def test_endpoints_cannot_be_configured_in_toml(
        self, config_overlay: ConfigOverlay
    ) -> None:
        raw = config_overlay.tree(
            {"app.mcp.endpoints": {"all": {"tools": ["tool_name"]}}}
        )

        with pytest.raises(ConfigError, match="endpoints"):
            bind(raw, path="app", model=McpAppConfig)

    def test_endpoint_names_exported_tools_and_allows_card_override(self) -> None:
        endpoint = McpEndpoint(
            id=uuid4(),
            prefix_url="/dq/all",
            title="Data quality",
            tools=("pg_query",),
            overrides={
                "pg_query": {
                    "description": "Run a database query",
                    "inputSchema": {"properties": {"sql": {"type": "string"}}},
                }
            },
            roles=frozenset({"analyst"}),
        )

        assert endpoint.path_under("/mcp") == "/mcp/dq/all"
        assert endpoint.tools == ("pg_query",)

    def test_endpoint_rejects_wildcard_tools(self) -> None:
        with pytest.raises(ValueError, match="exact exported tools"):
            McpEndpoint(id=uuid4(), prefix_url="/all", title="All", tools=("*",))


def test_scalar_data_path_builds_the_instance_paths() -> None:
    raw = OmegaConf.create({"data": "/srv/boba/data"})

    paths = bind(raw, "data", DataConfig)

    assert paths.data("workspace") == Path("/srv/boba/data/workspace")


def test_scalar_data_path_rejects_a_relative_directory() -> None:
    raw = OmegaConf.create({"data": "relative/data"})

    with pytest.raises(ConfigError, match="expected an absolute path"):
        bind(raw, "data", DataConfig)


def test_instance_paths_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        DataConfig(data_dir="data")
