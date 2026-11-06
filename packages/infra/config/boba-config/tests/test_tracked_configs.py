"""Примеры конфигов MCP: TOML в каталоге экземпляра и явные пути артефактов."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import ClassVar

import pytest


class TrackedConfigs:
    """Примеры конфигов плагинов рядом с основным конфигом экземпляра MCP."""

    DIRECTORY: ClassVar[Path] = (
        Path(__file__).resolve().parents[5] / "compose/mcp/conf/plugins"
    )

    def files(self) -> list[Path]:
        return sorted(self.DIRECTORY.glob("*.conf"))


class TestTrackedConfigs:
    def test_configs_are_found(self) -> None:
        assert TrackedConfigs().files(), "MCP instance has no plugin config examples"

    @pytest.mark.parametrize(
        "path", TrackedConfigs().files(), ids=lambda path: path.stem
    )
    def test_artifact_paths_use_the_runtime_directory(self, path: Path) -> None:
        with path.open("rb") as body:
            config = tomllib.load(body)

        sandbox = config["sandbox"]
        for field in ("program", "rootfs"):
            assert sandbox[field].startswith("${oc.env:BOBA_RUNTIME}/"), (path, field)

        for mount in sandbox.get("mounts", ()):
            assert mount.startswith("${oc.env:BOBA_RUNTIME}/"), (path, mount)
