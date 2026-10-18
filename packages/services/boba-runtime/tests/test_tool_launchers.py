"""Выбор способа запуска по [tool_launcher] и проверки старта реализаций."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import OmegaConf
from pydantic import ValidationError

from boba.runtime.config import AppLayers
from boba.runtime.launchers import (
    NoLaunchers,
    ProcessLaunchers,
    ToolLaunchers,
    ZygoteLaunchers,
)
from boba.stand_core.context import CallStand
from boba.toolkit.chain import CallAmbient
from boba.toolkit.manifest import LaunchSpec
from boba.toolrun.process import ProcessToolCaller
from boba.workspace.launcher import LauncherInterpreter


def _process_section(workdir: Path) -> dict[str, object]:
    return {
        "tool_launcher": {
            "provider": "process",
            "workdir": str(workdir),
            "timeout_sec": 60,
            "channel_limit_bytes": 1_000_000,
            "stderr_tail_bytes": 4096,
            "kill_grace_sec": 1.0,
        }
    }


def test_process_without_the_section_launches_no_tool_sections(
    call_stand: CallStand,
) -> None:
    raw = OmegaConf.create({})

    launchers = ToolLaunchers(raw).build()

    assert isinstance(launchers, NoLaunchers)

    launchers.probe()
    with pytest.raises(
        RuntimeError, match=r"'fake' cannot be launched.*\[tool_launcher\]"
    ):
        launchers.launcher_of(
            LaunchSpec(section="fake"), call_stand.contexts, CallAmbient()
        )


def _sandbox_section() -> dict[str, object]:
    """Способ запуска песочницей и интерпретатор процесса, как их кладёт AppLayers."""
    interpreter = AppLayers.interpreter().model_dump(mode="json")

    return {
        "tool_launcher": {"provider": "sandbox"},
        LauncherInterpreter.SECTION: interpreter,
    }


def test_sandbox_provider_builds_zygote_launchers() -> None:
    raw = OmegaConf.create(_sandbox_section())

    launchers = ToolLaunchers(raw).build()

    assert isinstance(launchers, ZygoteLaunchers)


def test_sandbox_probe_requires_env_paths() -> None:
    raw = OmegaConf.create(_sandbox_section())

    launchers = ToolLaunchers(raw).build()

    # конвенции песочницы стоят на путях [env]: без них probe отказывает
    with pytest.raises(ValidationError):
        launchers.probe()


def test_process_provider_builds_process_launchers(
    call_stand: CallStand, tmp_path: Path
) -> None:
    raw = OmegaConf.create(_process_section(tmp_path))

    launchers = ToolLaunchers(raw).build()

    assert isinstance(launchers, ProcessLaunchers)

    launchers.probe()
    launcher = launchers.launcher_of(
        LaunchSpec(section="fake"), call_stand.contexts, CallAmbient()
    )

    assert isinstance(launcher, ProcessToolCaller)


def test_process_probe_requires_existing_workdir(tmp_path: Path) -> None:
    raw = OmegaConf.create(_process_section(tmp_path / "absent"))

    launchers = ToolLaunchers(raw).build()

    with pytest.raises(RuntimeError, match="workdir"):
        launchers.probe()
