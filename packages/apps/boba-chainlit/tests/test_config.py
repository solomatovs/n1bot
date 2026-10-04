"""Конфиг приложения обязан разбираться моделями.

Проверяется тот же файл, с которым работает приложение: путь берётся из
BOBA_CONFIG_PATH, как и в остальных тестах. Правки структуры (новая секция,
обязательное поле, лимит профиля) ловятся здесь, а не при старте.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.chainlit.infra.config import AppConfig
from boba.config import bind
from boba.runtime.plugins import PluginMeta
from boba.stand.sandbox import section_profile

HEAVY_SECTIONS = ("tool.ingest", "tool.kb")
"""Секции с нативным инференсом: их душит квота базового профиля."""

BASE_CPU_PERCENT = 100
"""Квота базового профиля: одного ядра тяжёлым движкам мало."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestConfigStaysValid:
    """Конфиг разбирается моделями приложения: пропущенное поле — падение."""

    def test_app_section_binds(self, raw_config: DictConfig) -> None:
        bind(raw_config, path="app", model=AppConfig)

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
