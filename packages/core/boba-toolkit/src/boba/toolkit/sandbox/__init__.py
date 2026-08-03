"""Компонент песочницы: профили, сборка argv и запуск команд."""

from boba.toolkit.sandbox.argv import WORKSPACE_MOUNT, build_bwrap_argv
from boba.toolkit.sandbox.call_context import ToolCallContext
from boba.toolkit.sandbox.caller import SandboxCaller
from boba.toolkit.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.toolkit.sandbox.config import (
    SandboxConfig,
    SandboxToolConfig,
)
from boba.toolkit.sandbox.diagnostics import SandboxDiagnostics
from boba.toolkit.sandbox.payload import SandboxPayload, SandboxPayloadError
from boba.toolkit.sandbox.profile import BindSpec, SandboxProfile, TmpfsSpec
from boba.toolkit.sandbox.runner import SandboxOutcome, SandboxRunner

__all__ = [
    "WORKSPACE_MOUNT",
    "BindSpec",
    "CgroupError",
    "CgroupManager",
    "GroupLimits",
    "SandboxCaller",
    "SandboxConfig",
    "SandboxDiagnostics",
    "SandboxOutcome",
    "SandboxPayload",
    "SandboxPayloadError",
    "SandboxProfile",
    "SandboxRunner",
    "SandboxToolConfig",
    "TmpfsSpec",
    "ToolCallContext",
    "build_bwrap_argv",
]
