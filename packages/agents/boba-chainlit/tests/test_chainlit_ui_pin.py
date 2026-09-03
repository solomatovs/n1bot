"""Пин фронта chainlit: overlay собран под исходники одной версии, и она обязана
совпадать с установленным пакетом и с пином в pyproject. Апгрейд chainlit без
пересмотра overlay падает здесь, а не в проде.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import ClassVar

import chainlit
import pytest


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Тест читает файлы пакета: сессия чата ему не нужна."""


class UpstreamPin:
    """Версия исходников фронта, под которую написан overlay web/chainlit-ui."""

    PACKAGE_DIR: ClassVar[Path] = Path(__file__).resolve().parents[1]
    UPSTREAM: ClassVar[Path] = PACKAGE_DIR / "web" / "chainlit-ui" / "UPSTREAM"
    PYPROJECT: ClassVar[Path] = PACKAGE_DIR / "pyproject.toml"
    DEPENDENCY: ClassVar[str] = "chainlit=="

    @classmethod
    def version(cls) -> str:
        return cls.UPSTREAM.read_text(encoding="utf-8").strip()

    @classmethod
    def pinned_dependency(cls) -> str:
        """Пин chainlit из pyproject; отсутствие точного пина — ошибка."""
        with cls.PYPROJECT.open("rb") as handle:
            project = tomllib.load(handle)

        for dependency in project["project"]["dependencies"]:
            if dependency.startswith(cls.DEPENDENCY):
                return dependency.removeprefix(cls.DEPENDENCY)

        raise AssertionError("pyproject has no exact chainlit pin")


def test_installed_chainlit_matches_upstream_sources() -> None:
    assert chainlit.__version__ == UpstreamPin.version()


def test_pyproject_pins_upstream_version() -> None:
    assert UpstreamPin.pinned_dependency() == UpstreamPin.version()
