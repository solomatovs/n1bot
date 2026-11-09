"""Программа каждого плагина сервиса отдаёт манифест по секции из конфига
boba-mcp: `python -m boba.sandbox.program <id> manifest`, как её зовёт сервер
в режиме process."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.sandbox.program import PluginCommand, PluginEntry
from boba.toolkit.manifest import PluginManifest

PLUGINS_DIR = (
    Path(__file__).resolve().parents[4] / "compose" / "mcp" / "conf" / "plugins"
)
"""Конфиги плагинов сервиса: по одному на плагин, имя файла — id."""


def _ids() -> list[str]:
    ids: list[str] = []
    for path in sorted(PLUGINS_DIR.glob("*.conf")):
        ids.append(path.stem)

    assert ids, f"No plugin configs found in {PLUGINS_DIR}"
    return ids


def _section(raw: DictConfig, plugin_id: str) -> dict[str, Any]:
    node = OmegaConf.select(raw, f"tool.{plugin_id}")
    container = OmegaConf.to_container(node, resolve=True)

    section: dict[str, Any] = {}
    if isinstance(container, dict):
        for key, value in container.items():
            section[str(key)] = value

    return section


@pytest.mark.parametrize("plugin_id", _ids())
def test_program_prints_the_manifest_of_the_config(
    service_raw_config: DictConfig, plugin_id: str
) -> None:
    section = _section(service_raw_config, plugin_id)
    argv = (*PluginEntry().argv(sys.executable, plugin_id), PluginCommand.MANIFEST)

    done = subprocess.run(
        argv,
        input=json.dumps(section).encode("utf-8"),
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr.decode()
    manifest = PluginManifest.model_validate_json(done.stdout)
    assert manifest.id == plugin_id

    assert manifest.tools
