"""boba-tool-shell — плагин с двумя bash-tool'ами.

Entry-point модуль для `AgentBuilder.use_plugin(boba.tool.shell)` /
discovery через `[project.entry-points."boba.plugins"]`.

Экспортирует на module-scope сами `@tool`-функции — `AgentBuilder`
обходит `dir(module)`, забирает помеченные объекты, оборачивает
в `DishkaTool`, регистрирует в DI (если предикат `enable_if`
вернул `True`).

`bash_local` ↔ `[tool.bash_local]` / `BOBA_TOOL__BASH_LOCAL__*`.
`bash_sandbox` ↔ `[tool.bash_sandbox]` / `BOBA_TOOL__BASH_SANDBOX__*`.
"""

from __future__ import annotations

from boba.tool.shell.bash_local import bash_local
from boba.tool.shell.bash_sandbox import bash_sandbox
from boba.tool.shell.config import BashLocalConfig, BashSandboxConfig

__all__ = [
    "BashLocalConfig",
    "BashSandboxConfig",
    "bash_local",
    "bash_sandbox",
]
