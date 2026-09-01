"""Парсеры документов живут в песочнице: процесс приложения их не импортирует."""

import subprocess
import sys

import pytest


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Проверка идёт отдельным процессом: сессия чата не нужна."""


class TestParsersStayInSandbox:
    """Приложение не тянет тяжёлые парсеры: они живут в телах инструментов."""

    @pytest.mark.parametrize(
        "module", ["liteparse", "markdownify", "bs4", "lxml", "plotly"]
    )
    def test_app_does_not_import(self, module: str) -> None:
        code = (
            "import sys\n"
            "import boba.chainlit.infra.plugins\n"
            f"if {module!r} in sys.modules:\n"
            f"    raise SystemExit('the app pulls {module}')\n"
            "print('ok')\n"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
