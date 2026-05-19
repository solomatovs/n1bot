"""boba-tool-shell — v2 плагин с двумя bash-tool'ами.

Entry-point модуль для `AgentBuilder.use_plugin(boba.tool.shell)` /
discovery через `[project.entry-points."boba.plugins"]`.

Экспортирует на module-scope сами `@tool`-классы — `AgentBuilder`
обходит `dir(module)`, забирает помеченные объекты, оборачивает
в `DishkaTool`, регистрирует в DI (если предикат `enable_if`
вернул `True`).

`BashLocalTool` ↔ `[tool.bash_local]` / `BOBA_TOOL__BASH_LOCAL__*`.
`BashSandboxTool` ↔ `[tool.bash_sandbox]` / `BOBA_TOOL__BASH_SANDBOX__*`.
"""

from __future__ import annotations

from boba.tool.shell.bash_local import BashLocalTool
from boba.tool.shell.bash_sandbox import BashSandboxTool
from boba.tool.shell.config import BashLocalConfig, BashSandboxConfig

__all__ = [
    "BashLocalConfig",
    "BashLocalTool",
    "BashSandboxConfig",
    "BashSandboxTool",
]
