"""Pytest fixtures для пакета boba-tool-kb (integration-mode).

pytest -m integration для запуска; default-режим (-m "not integration")
их исключает (см. root pyproject.toml).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.settings import bind, build_app_config
from boba.tool.kb.confluence.list_spaces import ConfluenceListSpacesConfig


@pytest.fixture
def confluence_list_spaces_cfg() -> ConfluenceListSpacesConfig:
    try:
        # получаем настройки всего приложения
        if (config_path := os.environ.get("BOBA_CONFIG_PATH")) is None:
            raise ValueError("please pass env BOBA_CONFIG_PATH")

        return bind(
            build_app_config(config_path=Path(config_path), argv=sys.argv[1:]),
            "tool.kb",
            ConfluenceListSpacesConfig,
        )
    except ValidationError as e:
        pytest.skip(
            f"[tool.kb.confluence.list.spaces] не сконфигурирован: {e}",
        )
