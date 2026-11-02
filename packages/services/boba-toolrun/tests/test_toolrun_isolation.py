"""Автономность конвейера обёрток: он не зависит от остального приложения.

Обёртки получают всё внешнее параметрами (роли, приёмник живого вывода,
исполнитель), поэтому импортируют boba-toolkit, общий I/O-слой boba-tool-io и
соседние пакеты ядра; приложений в ядре запуска нет. Тест держит это
свойство: пока оно есть, подпакет переносится в отдельный пакет механически.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from boba.stand_core.deps import ImportScan

TOOLRUN = Path(__file__).resolve().parents[1] / "src/boba/toolrun"

MODULES = sorted(p for p in TOOLRUN.glob("*.py") if p.name != "__init__.py")


class TestToolrunIsolation:
    ALLOWED_PREFIXES: ClassVar[tuple[str, ...]] = (
        "boba.toolrun",
        "boba.toolkit",
        "boba.tool_io",
        "boba.access",
        "boba.cancellation",
        "boba.identity",
        "boba.canvas",
        "boba.messaging",
    )

    @pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
    def test_no_application_imports(self, module: Path) -> None:
        """Импорт из chainlit вернёт зависимость, которую мы только что сняли."""
        scan = ImportScan()

        leaked: list[str] = []
        for name in scan.modules_of(module):
            if not name.startswith("boba."):
                continue

            if name.startswith(self.ALLOWED_PREFIXES):
                continue

            leaked.append(name)

        if leaked:
            raise AssertionError(f"{module.name}: внешние зависимости {leaked}")
