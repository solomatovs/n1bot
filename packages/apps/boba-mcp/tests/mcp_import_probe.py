"""Сервис boba-mcp под аудитом импорта: строка в stderr на каждый модуль
кода плагинов (boba.tool.*), который импортирует сам процесс сервиса.

Запуск: `python mcp_import_probe.py --config <toml> [--config <наложение>]` — те же
аргументы, что у boba-mcp; вход сервиса тот же. Хук ставится до импорта
сервиса, поэтому видит и импорты на импорте модулей.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from typing import Any, ClassVar


class PluginImports:
    """Аудит-хук импорта: метка MARK и имя модуля кода плагинов в stderr.

    Ставит его вход пробы через sys.addaudithook; тест ищет метку в логе
    процесса. Подпроцессы (программы плагинов) хук не наследуют.
    """

    PREFIX: ClassVar[str] = "boba.tool."
    MARK: ClassVar[str] = "plugin module imported by the service: "

    def __call__(self, event: str, args: tuple[Any, ...]) -> None:
        if event != "import":
            return

        name = str(args[0])
        if not name.startswith(self.PREFIX):
            return

        print(f"{self.MARK}{name}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.addaudithook(PluginImports())
    app = importlib.import_module("boba.mcp_server.app")
    asyncio.run(app.main())
