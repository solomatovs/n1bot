"""Shell-инструмент в изоляции bubblewrap: профили, namespace'ы, лимиты."""

from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig
from boba.chainlit2.agent.tools.sandbox.tools import build_bash_tool, has_bwrap
from boba.chainlit2.sandbox.argv import WORKSPACE_MOUNT
from boba.chainlit2.sandbox.profile import (
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
