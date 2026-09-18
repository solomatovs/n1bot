"""Сборка набора плагина describer: узлы и рёбра из своих модулей.

Каждая сущность живёт в своём модуле (nodes, edges) с собственными
инструментами и картой EXPECTED; здесь только общий TOOLS для манифеста.
Запуск: `python -m boba.tool.describer.tools <имя> --флаги`.

Ошибок своих не выпускает: отказы объявляют модули сущностей.
"""

from __future__ import annotations

import sys
from typing import Final

from boba.tool.describer.edges import TOOLS as EDGE_TOOLS
from boba.tool.describer.nodes import TOOLS as NODE_TOOLS
from boba.toolkit.entry import ToolMain

__all__ = ["TOOLS"]

TOOLS: Final = (*NODE_TOOLS, *EDGE_TOOLS)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
