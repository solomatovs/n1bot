"""Вход программы boba-mcp: служба и служебные подкоманды.

`boba-mcp serve|health --config <toml> [--config <наложение>]` — служба
(boba.mcp_server.app). Служебные подкоманды (ProgramTool) идут без службы и
её импортов: `launcher <опции>` — внешний слой цепочки песочницы
(boba.workspace.launcher), `probe` — пустой процесс пробы cgroup,
`self-check` — самопроверка собранного бинарника по описи сборки. Собранный
Nuitka бинарник начинается с этого модуля: у него нет `python -m`, и
песочница зовёт его подкомандами.

Ошибки:
SystemExit — подкоманда не удалась: код выхода лаунчера либо текст причины.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from boba.core.package_data import PackageDataCheck, PackageDataError
from boba.runtime.program import ProgramExit, ProgramTool
from boba.workspace.launcher import Launcher

__all__ = ["McpProgram", "cli"]


class McpProgram:
    """Служебные подкоманды бинарника boba-mcp.

    Создаётся входом cli(): он отдаёт сюда командную строку, когда её первое
    слово — служебная подкоманда, остальное уходит службе. Лаунчер
    исполняется в главном потоке процесса без цикла событий: его fuse2fs
    живёт по pdeathsig потока, а --exec заменяет собой процесс.
    """

    def __init__(self) -> None:
        self._check = PackageDataCheck()

    def serves(self, argv: Sequence[str]) -> bool:
        """Командная строка службы: не служебная подкоманда."""
        if not argv:
            return True

        for tool in ProgramTool:
            if argv[0] == tool:
                return False

        return True

    def run(self, argv: Sequence[str]) -> int:
        match argv:
            case [ProgramTool.LAUNCHER, *options]:
                return Launcher.main(list(options))
            case [ProgramTool.PROBE]:
                return ProgramExit.OK
            case [ProgramTool.SELF_CHECK]:
                return self._self_check()
            case _:
                msg = (
                    f"boba-mcp: {' '.join(argv)!r} is not a command, expected "
                    "serve|health --config <toml>, launcher <options>, probe or "
                    "self-check"
                )
                raise SystemExit(msg)

    def _self_check(self) -> int:
        try:
            checked = self._check.run()
        except PackageDataError as exc:
            raise SystemExit(str(exc)) from exc

        sys.stdout.write(f"{checked}\n")

        return ProgramExit.OK


def cli() -> None:
    argv = sys.argv[1:]
    program = McpProgram()
    if not program.serves(argv):
        sys.exit(program.run(argv))

    # служба грузит сеть, вход и реестр инструментов: служебные подкоманды
    # стартуют без этих импортов
    from boba.mcp_server.app import main  # noqa: PLC0415

    asyncio.run(main())


if __name__ == "__main__":
    cli()
