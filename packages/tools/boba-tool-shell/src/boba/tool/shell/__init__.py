"""Shell-инструмент в изоляции bubblewrap: профили, namespace'ы, лимиты."""

from boba.tool.shell.config import BashSandboxConfig
from boba.tool.shell.tools import build_bash_tool, has_bwrap
from boba.toolkit.sandbox.argv import WORKSPACE_MOUNT
from boba.toolkit.sandbox.profile import (
    BindSpec,
    SandboxProfile,
    TmpfsSpec,
)

__all__ = [
    "WORKSPACE_MOUNT",
    "BashSandboxConfig",
    "BindSpec",
    "SandboxProfile",
    "TmpfsSpec",
    "build_bash_tool",
    "has_bwrap",
]
