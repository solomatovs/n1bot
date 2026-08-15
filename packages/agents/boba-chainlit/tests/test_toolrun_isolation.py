"""Автономность конвейера обёрток: он не зависит от остального приложения.

Обёртки получают всё внешнее параметрами (роли, приёмник живого вывода,
исполнитель), поэтому импортируют только langchain, boba-toolkit и
boba-cancellation. Тест держит это свойство: пока оно есть, подпакет
переносится в отдельный пакет механически.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

TOOLRUN = Path(__file__).resolve().parents[1] / "src/boba/chainlit/agent/toolrun"

MODULES = sorted(p for p in TOOLRUN.glob("*.py") if p.name != "__init__.py")


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Проверка читает исходники: сессия приложения этому тесту не нужна."""


class TestToolrunIsolation:
    ALLOWED_PREFIXES: ClassVar[tuple[str, ...]] = (
        "boba.chainlit.agent.toolrun",
        "boba.toolkit",
        "boba.cancellation",
        "langchain_core",
    )

    @staticmethod
    def _imported_modules(source: str) -> list[str]:
        names: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
                continue

            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.append(alias.name)

        return names

    @pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
    def test_no_application_imports(self, module: Path) -> None:
        """Импорт из chainlit вернёт зависимость, которую мы только что сняли."""
        leaked: list[str] = []
        for name in self._imported_modules(module.read_text()):
            if not name.startswith("boba."):
                continue

            if name.startswith(self.ALLOWED_PREFIXES):
                continue

            leaked.append(name)

        assert not leaked, f"{module.name}: внешние зависимости {leaked}"
