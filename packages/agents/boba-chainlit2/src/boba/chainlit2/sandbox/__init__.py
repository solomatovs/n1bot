"""Компонент песочницы: профили, сборка argv и запуск команд."""

from boba.chainlit2.sandbox.argv import WORKSPACE_MOUNT, build_bwrap_argv
from boba.chainlit2.sandbox.call_context import ToolCallContext
from boba.chainlit2.sandbox.caller import SandboxCaller
from boba.chainlit2.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.chainlit2.sandbox.config import (
    SandboxConfig,
    SandboxEntryConfig,
    SandboxToolConfig,
)
from boba.chainlit2.sandbox.diagnostics import SandboxDiagnostics
from boba.chainlit2.sandbox.payload import SandboxPayload, SandboxPayloadError
from boba.chainlit2.sandbox.profile import BindSpec, SandboxProfile, TmpfsSpec
from boba.chainlit2.sandbox.runner import SandboxOutcome, SandboxRunner

__all__ = [
    "WORKSPACE_MOUNT",
    "BindSpec",
    "CgroupError",
    "CgroupManager",
    "GroupLimits",
    "SandboxCaller",
    "SandboxConfig",
    "SandboxDiagnostics",
    "SandboxEntryConfig",
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
