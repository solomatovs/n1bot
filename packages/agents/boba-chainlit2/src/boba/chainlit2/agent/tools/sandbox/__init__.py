"""Shell-инструмент в изоляции bubblewrap: профили, namespace'ы, лимиты."""

from boba.chainlit2.agent.tools.sandbox.argv import WORKSPACE_MOUNT
from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig
from boba.chainlit2.agent.tools.sandbox.profile import SandboxProfile
from boba.chainlit2.agent.tools.sandbox.tools import build_bash_tool, has_bwrap

__all__ = [
    "WORKSPACE_MOUNT",
    "BashSandboxConfig",
    "SandboxProfile",
    "build_bash_tool",
    "has_bwrap",
]
