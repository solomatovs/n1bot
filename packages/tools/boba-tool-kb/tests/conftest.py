"""Pytest fixtures для пакета boba-tool-kb (integration-mode).

`pytest -m integration` для запуска; default-режим (`-m "not integration"`)
их исключает (см. root pyproject.toml).
"""
# pyright: reportCallIssue=false
# BobaFlatSettings()-вызовы для config-фикстур: pyright не видит, что поля
# заполняются source-loader'ом из TOML, и считает обязательные аргументы
# отсутствующими. Это runtime-фича boba.settings — статически не выразимо.

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.tool.kb.confluence.tools.list.spaces import ConfluenceListSpacesConfig


@pytest.fixture
def confluence_list_spaces_cfg() -> ConfluenceListSpacesConfig:
    try:
        return ConfluenceListSpacesConfig()
    except ValidationError as e:
        pytest.skip(
            f"[tool.kb.confluence.list.spaces] не сконфигурирован: {e}",
        )
