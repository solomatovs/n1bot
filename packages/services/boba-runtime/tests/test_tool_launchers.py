"""Выбор способа запуска по [tool_launcher] и проверки старта реализаций."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from boba.runtime.launchers import (
    ProcessLaunchers,
    ToolLaunchers,
    ZygoteLaunchers,
)
from boba.toolrun.process import ProcessToolCaller


def _process_section(workdir: Path) -> dict[str, object]:
    return {
        "tool_launcher": {
            "provider": "process",
            "workdir": str(workdir),
            "shell": "/bin/bash",
            "timeout_sec": 60,
            "channel_limit_bytes": 1_000_000,
            "stderr_tail_bytes": 4096,
            "kill_grace_sec": 1.0,
        }
    }


def test_missing_section_is_refused() -> None:
    raw = OmegaConf.create({})

    with pytest.raises(RuntimeError, match=r"\[tool_launcher\] is required"):
        ToolLaunchers.of(raw)


def test_sandbox_provider_builds_zygote_launchers() -> None:
    raw = OmegaConf.create({"tool_launcher": {"provider": "sandbox"}})

    launchers = ToolLaunchers.of(raw)

    assert isinstance(launchers, ZygoteLaunchers)


def test_sandbox_probe_requires_sandbox_section() -> None:
    raw = OmegaConf.create({"tool_launcher": {"provider": "sandbox"}})

    launchers = ToolLaunchers.of(raw)

    with pytest.raises(RuntimeError, match=r"\[sandbox\] is required"):
        launchers.probe()


def test_process_provider_builds_process_launchers(tmp_path: Path) -> None:
    raw = OmegaConf.create(_process_section(tmp_path))

    launchers = ToolLaunchers.of(raw)

    assert isinstance(launchers, ProcessLaunchers)

    launchers.probe()
    launcher = launchers.launcher_of("fake", ())

    assert isinstance(launcher, ProcessToolCaller)


def test_process_probe_requires_existing_workdir(tmp_path: Path) -> None:
    raw = OmegaConf.create(_process_section(tmp_path / "absent"))

    launchers = ToolLaunchers.of(raw)

    with pytest.raises(RuntimeError, match="workdir"):
        launchers.probe()
