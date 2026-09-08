"""Конфиг процесса studio с секциями, которых общий runtime не знает: каталог.

Ошибки:
ValueError — секция [catalog] не разбирается (pydantic через bind).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import ConfigDict, Field

from boba.catalog_service import CatalogConfig
from boba.runtime.config import StudioRuntimeConfig

__all__ = ["StudioAppConfig"]


class StudioAppConfig(StudioRuntimeConfig):
    """Секции studio: общие, [studio] и [catalog]."""

    model_config = ConfigDict(extra="ignore")

    catalog: Annotated[
        CatalogConfig,
        Field(description="Секция [catalog]: таблицы каталога данных и роли доступа."),
    ]
