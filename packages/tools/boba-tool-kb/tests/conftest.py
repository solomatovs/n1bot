"""Pytest fixtures для пакета boba-tool-kb (integration-mode).

`pytest -m integration` для запуска; default-режим (`-m "not integration"`)
их исключает (см. root pyproject.toml).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.settings import bind, build_app_config
from boba.tool.kb.confluence.list_spaces import ConfluenceListSpacesConfig


@pytest.fixture
def confluence_list_spaces_cfg() -> ConfluenceListSpacesConfig:
    try:
        return bind(build_app_config(), "tool.kb", ConfluenceListSpacesConfig)
    except ValidationError as e:
        pytest.skip(
            f"[tool.kb.confluence.list.spaces] не сконфигурирован: {e}",
        )
