"""Конфиг сервиса обязан разбираться моделями.

Проверяется тот же файл, с которым работает сервис: его секции инструментов
и профили песочницы. Правки структуры (новая секция, обязательное поле, лимит
профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.config import ConfigError, bind
from boba.mcp_server.app import McpAppConfig
from boba.runtime.plugins import PluginMeta
from boba.stand.overlay import ConfigOverlay
from boba.stand.sandbox import section_profile

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


class TestStaticConnectionConfigIsRemoved:
    """Сервис принимает профили connections только из своего каталога БД."""

    def test_profiles_cannot_be_configured_in_toml(
        self, config_overlay: ConfigOverlay
    ) -> None:
        raw = config_overlay.tree(
            {"app.connections": {"confluence": {"host": "example.org"}}}
        )

        with pytest.raises(ConfigError, match="connections"):
            bind(raw, path="app", model=McpAppConfig)

    @pytest.mark.parametrize("source", ["server", "sealed"])
    def test_obsolete_connection_sources_are_refused(
        self, config_overlay: ConfigOverlay, source: str
    ) -> None:
        path = "mcp.endpoints.all.connections"
        rules = [{"roles": ["*"], "offer": [source]}]
        raw = config_overlay.tree({path: rules})

        with pytest.raises(ConfigError, match="offer"):
            bind(raw, path="app", model=McpAppConfig)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("offer", ["shared"]),
            ("client", "any"),
            ("offer", ["stored", "sealed"]),
        ],
        ids=["unknown-source", "removed-client-selector", "removed-profile-source"],
    )
    def test_unknown_source_or_client_of_a_rule_is_refused(
        self,
        raw_config: DictConfig,
        config_overlay: ConfigOverlay,
        key: str,
        value: object,
    ) -> None:
        path = "mcp.endpoints.all.connections"
        rules = OmegaConf.to_container(
            OmegaConf.select(raw_config, path), resolve=False
        )
        assert isinstance(rules, list)
        rule = rules[0]
        assert isinstance(rule, dict)
        rule[key] = value
        raw = config_overlay.tree({path: rules})

        with pytest.raises(ConfigError, match=key):
            bind(raw, path="app", model=McpAppConfig)
