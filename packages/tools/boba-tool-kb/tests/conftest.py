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

from boba.tool.kb.confluence.tools.spaces_list import ConfluenceSpacesListConfig


@pytest.fixture
def confluence_spaces_list_cfg() -> ConfluenceSpacesListConfig:
    try:
        return ConfluenceSpacesListConfig()
    except ValidationError as e:
        pytest.skip(
            f"[tool.kb.confluence.spaces_list] не сконфигурирован: {e}",
        )
