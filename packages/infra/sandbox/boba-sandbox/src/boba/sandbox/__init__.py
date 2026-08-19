"""Компонент песочницы: профили, сборка argv и запуск команд."""

from boba.sandbox.argv import WORKSPACE_MOUNT, build_bwrap_argv
from boba.sandbox.caller import (
    SandboxCaller,
    SandboxPayloadError,
)
from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.premount import RootfsPremount, RootfsPremountError
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
    "WORKSPACE_MOUNT",
    "BindSpec",
    "CgroupError",
    "CgroupManager",
    "GroupLimits",
    "RootfsPremount",
    "RootfsPremountError",
    "SandboxCaller",
    "SandboxConfig",
    "SandboxDiagnostics",
    "SandboxLaunchError",
    "SandboxMountError",
    "SandboxOutcome",
    "SandboxPayloadError",
    "SandboxProfile",
    "SandboxRunner",
    "SandboxToolConfig",
    "TmpfsSpec",
    "ZygotePolicy",
    "ZygoteRegistry",
    "ZygoteSpawner",
    "ZygoteSupervisor",
    "ZygoteToolCaller",
    "build_bwrap_argv",
    "has_bwrap",
]
