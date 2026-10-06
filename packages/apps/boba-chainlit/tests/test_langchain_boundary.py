"""Граница с langchain: его типы знает только каталог boba/chainlit/agent.

Граф хода, модель, хранилище checkpoint'ов и колбэки прогонов живут на
langchain и langgraph, но остальному чату они отдают и принимают типы
диалога (boba.chainlit.chat.dialog). Тест держит это свойство: импорт
langchain вне agent вернёт его типы в данные чата.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

CHAT = Path(__file__).resolve().parents[1] / "src/boba/chainlit"
AGENT = CHAT / "agent"

MODULES = sorted(p for p in CHAT.rglob("*.py") if AGENT not in p.parents)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Исходники читаются с диска: сессия chainlit проверке не нужна."""


class TestLangchainBoundary:
    FORBIDDEN: ClassVar[tuple[str, ...]] = (
        "langchain",
        "langgraph",
        "chainlit.langchain",
    )
    """Пакеты langchain и langgraph с любым суффиксом имени (langchain_core,
    langgraph.checkpoint) и мост chainlit к langchain: он тянет его за собой."""

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

    def test_the_chat_has_modules_outside_the_agent(self) -> None:
        """Пустой список модулей сделал бы проверку ниже пустой."""
        if len(MODULES) < 10:
            raise AssertionError(f"chat modules outside agent: {MODULES}")

        if not list(AGENT.glob("*.py")):
            raise AssertionError(f"agent directory {AGENT} has no modules")

    @pytest.mark.parametrize("module", MODULES, ids=lambda p: str(p.relative_to(CHAT)))
    def test_no_langchain_outside_the_agent(self, module: Path) -> None:
        leaked: list[str] = []
        for name in self._imported_modules(module.read_text()):
            if not name.startswith(self.FORBIDDEN):
                continue

            leaked.append(name)

        if leaked:
            raise AssertionError(
                f"{module.relative_to(CHAT)}: langchain is imported outside "
                f"boba/chainlit/agent: {leaked}"
            )
