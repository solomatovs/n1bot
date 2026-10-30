"""Компонент песочницы: профили, сборка argv и запуск команд."""

from boba.sandbox.cgroup import CgroupError, CgroupManager, CgroupProbe, GroupLimits
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.profile import (
    BindSpec,
    SandboxConfig,
    SandboxProfile,
    SandboxToolConfig,
    TmpfsSpec,
    WorkspaceSpec,
)
from boba.sandbox.runner import (
    SandboxLaunchError,
    SandboxMountError,
    has_bwrap,
)
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteSpawner,
    ZygoteSupervisor,
    ZygoteToolCaller,
)

__all__ = [
    "BindSpec",
    "CgroupError",
    "CgroupManager",
    "CgroupProbe",
    "GroupLimits",
    "SandboxConfig",
    "SandboxDiagnostics",
    "SandboxLaunchError",
    "SandboxMountError",
    "SandboxProfile",
    "SandboxToolConfig",
    "TmpfsSpec",
    "WorkspaceSpec",
    "ZygotePolicy",
    "ZygoteRegistry",
    "ZygoteSpawner",
    "ZygoteSupervisor",
    "ZygoteToolCaller",
    "has_bwrap",
]
