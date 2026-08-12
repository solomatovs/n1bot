"""Компонент песочницы: профили, сборка argv и запуск команд."""

from boba.sandbox.argv import WORKSPACE_MOUNT, ChannelArgv, WrapArgsCodec
from boba.sandbox.caller import (
    SandboxCaller,
    SandboxPayloadError,
)
from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.diagnostics import FailureFacts, SandboxDiagnostics
from boba.sandbox.journal import (
    CallJournal,
    DirVault,
    JournalError,
    JournalListener,
    StreamJournal,
    StreamJournalHub,
    StreamSlice,
)
from boba.sandbox.profile import (
    BindSpec,
    SandboxConfig,
    SandboxProfile,
    SandboxToolConfig,
    TmpfsSpec,
)
from boba.sandbox.runner import (
    ToolCallContext,
    has_bwrap,
)
from boba.sandbox.workflow import StageDef, StageRegistry, WorkflowRunner

__all__ = [
    "WORKSPACE_MOUNT",
    "BindSpec",
    "CallJournal",
    "CgroupError",
    "CgroupManager",
    "ChannelArgv",
    "DirVault",
    "FailureFacts",
    "GroupLimits",
    "JournalError",
    "JournalListener",
    "SandboxCaller",
    "SandboxConfig",
    "SandboxDiagnostics",
    "SandboxPayloadError",
    "SandboxProfile",
    "SandboxToolConfig",
    "StageDef",
    "StageRegistry",
    "StreamJournal",
    "StreamJournalHub",
    "StreamSlice",
    "TmpfsSpec",
    "ToolCallContext",
    "WorkflowRunner",
    "WrapArgsCodec",
    "has_bwrap",
]
