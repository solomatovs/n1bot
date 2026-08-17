"""Шаблон конфига релиза обязан оставаться рабочим.

Рабочий config.toml в репозиторий не входит, поэтому правки его структуры
(новая секция, обязательное поле, лимит профиля) в шаблон не доезжают сами —
их ловит этот тест: шаблон собирается тем же build_app_config и валидируется
теми же моделями, что и боевой конфиг.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.plugins import PluginMeta
from boba.settings import bind, build_app_config

REPO = Path(__file__).resolve().parents[4]
EXAMPLE = REPO / "build" / "conf" / "config.toml.example"

HEAVY_SECTIONS = ("tool.ingest", "tool.kb")
"""Секции с нативным инференсом: их душит квота базового профиля."""

BASE_CPU_PERCENT = 100
"""Квота базового профиля: одного ядра тяжёлым движкам мало."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


PLACEHOLDER = "example"
"""Чем заполняются пустые site-поля: проверяется структура, а не значения."""


@pytest.fixture(scope="module")
def example_config() -> DictConfig:
    """Шаблон с тем же окружением, что и боевой конфиг приложения.

    Секция [site] в шаблоне пустая — её заполняют при развёртывании; для
    проверки структуры пустые значения подменяются заглушкой.
    """
    if not EXAMPLE.is_file():
        pytest.fail(f"шаблон конфига пропал: {EXAMPLE}")

    env = {
        "BOBA_BASE": str(REPO / "compose"),
        "BOBA_APP": str(REPO / "packages"),
        "BOBA_PORT": "8601",
        "BOBA_URL_PREFIX": "/boba",
        "BOBA_CGROUP_BASE": "/sys/fs/cgroup/boba",
    }
    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        built = build_app_config(config_path=EXAMPLE)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
                continue

            os.environ[name] = value

    _fill_site(built)
    return built


def _fill_site(config: DictConfig) -> None:
    """Пустые строки секции [site] -> заглушка: иначе не пройдут валидаторы."""
    site = OmegaConf.select(config, "site")
    if site is None:
        pytest.fail("в шаблоне нет секции [site]")

    for key in site:
        value = OmegaConf.select(site, str(key))
        if value != "":
            continue

        OmegaConf.update(site, str(key), PLACEHOLDER)


class TestExampleStaysValid:
    """Шаблон разбирается моделями приложения: пропущенное поле — падение."""

    def test_app_section_binds(self, example_config: DictConfig) -> None:
        app = bind(example_config, path="app", model=AppConfig)

        if not (app.sandbox.profiles):
            raise AssertionError("app.sandbox.profiles")

    def test_every_tool_section_binds_meta(self, example_config: DictConfig) -> None:
        tools = OmegaConf.select(example_config, "tool")
        if not (tools):
            raise AssertionError("в шаблоне нет ни одной секции [tool.*]")

        for name in tools:
            bind(example_config, path=f"tool.{name}", model=PluginMeta)


class TestHeavyToolsGetTheirCpu:
    """Нативный инференс под квотой базового профиля идёт в разы дольше:
    onnxruntime считает ядра по хосту, а получает долю (см. CpuBudget)."""

    @pytest.mark.parametrize("section", HEAVY_SECTIONS)
    def test_cpu_quota_is_raised(
        self, example_config: DictConfig, section: str
    ) -> None:
        quota: Any = OmegaConf.select(
            example_config, f"{section}.sandbox.override.cgroup_cpu_percent"
        )

        if quota is None:
            raise AssertionError(
                f"[{section}.sandbox.override]: cgroup_cpu_percent не задан — "
                "секция унаследует одно ядро базового профиля"
            )
        if quota <= BASE_CPU_PERCENT:
            raise AssertionError(
                f"[{section}.sandbox.override]: cgroup_cpu_percent={quota} — "
                "эмбеддингу и OCR одного ядра мало"
            )
