"""Пин фронта chainlit: overlay собран под исходники одной версии, и она обязана
совпадать с исходной версией установленного wheel. Локальный патч закреплён
отдельно; переход на другой upstream без пересмотра overlay падает здесь.
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

    def version(self) -> str:
        return self.UPSTREAM.read_text(encoding="utf-8").strip()

    def pinned_dependency(self) -> str:
        """Пин chainlit из pyproject; отсутствие точного пина — ошибка."""
        with self.PYPROJECT.open("rb") as handle:
            project = tomllib.load(handle)

        for dependency in project["project"]["dependencies"]:
            if dependency.startswith(self.DEPENDENCY):
                return dependency.removeprefix(self.DEPENDENCY)

        raise AssertionError("pyproject has no exact chainlit pin")


def test_installed_chainlit_matches_upstream_sources() -> None:
    pin = UpstreamPin()
    assert chainlit.__version__ == pin.pinned_dependency()
    upstream, _, _ = chainlit.__version__.partition("+")
    assert upstream == pin.version()


def test_pyproject_pins_upstream_version() -> None:
    pin = UpstreamPin()
    assert pin.pinned_dependency() == f"{pin.version()}+boba.1"
