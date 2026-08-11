"""Компонент песочницы: профили, сборка argv и запуск команд."""

from boba.sandbox.argv import WORKSPACE_MOUNT, build_bwrap_argv
from boba.sandbox.caller import (
    SandboxCaller,
    SandboxPayload,
    SandboxPayloadError,
)
from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.profile import (
    BindSpec,
    SandboxConfig,
    SandboxProfile,
    SandboxToolConfig,
    TmpfsSpec,
)
from boba.sandbox.runner import (
    SandboxLaunchError,
    SandboxMountError,
    SandboxOutcome,
    SandboxRunner,
    ToolCallContext,
    has_bwrap,
)

__all__ = [
    "WORKSPACE_MOUNT",
    "BindSpec",
    "CgroupError",
    "CgroupManager",
    "GroupLimits",
    "SandboxCaller",
    "SandboxConfig",
    "SandboxDiagnostics",
    "SandboxLaunchError",
    "SandboxMountError",
    "SandboxOutcome",
    "SandboxPayload",
    "SandboxPayloadError",
    "SandboxProfile",
    "SandboxRunner",
    "SandboxToolConfig",
    "TmpfsSpec",
    "ToolCallContext",
    "build_bwrap_argv",
    "has_bwrap",
]
