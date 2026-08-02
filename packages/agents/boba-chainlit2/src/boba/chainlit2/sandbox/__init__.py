"""Компонент песочницы: профили, сборка argv и запуск команд."""

from boba.chainlit2.sandbox.argv import WORKSPACE_MOUNT, build_bwrap_argv
from boba.chainlit2.sandbox.config import SandboxConfig
from boba.chainlit2.sandbox.diagnostics import SandboxDiagnostics
from boba.chainlit2.sandbox.profile import BindSpec, SandboxProfile, TmpfsSpec
from boba.chainlit2.sandbox.runner import SandboxOutcome, SandboxRunner

__all__ = [
    "WORKSPACE_MOUNT",
    "BindSpec",
    "SandboxConfig",
    "SandboxDiagnostics",
    "SandboxOutcome",
    "SandboxProfile",
    "SandboxRunner",
    "TmpfsSpec",
    "build_bwrap_argv",
]
