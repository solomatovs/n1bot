"""Таблица плагинов studio: общая таблица процессов плюс инструменты каталога
данных, которым нужен сервис каталога из контейнера studio.

Ошибки:
RuntimeError — корневой контейнер не поднят, конфиг не studio.
"""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.tools import BaseTool

from boba.runtime import providers as runtime
from boba.runtime.di import Container
from boba.runtime.plugins import CoreTools, ToolPlugin
from boba.runtime.refs import RuntimeRefs
from boba.studio.catalog import providers as catalog
from boba.studio.catalog.tools import CatalogToolConfig, build_catalog_tools
from boba.studio.config import StudioAppConfig
from boba.toolkit.launcher import LauncherFactory

__all__ = ["StudioTools"]


class StudioTools:
    """Плагины процесса studio."""

    @classmethod
    def table(cls, refs: RuntimeRefs) -> Mapping[str, ToolPlugin]:
        table: dict[str, ToolPlugin] = dict(CoreTools.table(refs))
        table["catalog"] = ToolPlugin(
            section="catalog",
            config_model=CatalogToolConfig,
            build=cls._catalog,
            sandboxed=False,
        )

        return table

    @staticmethod
    def _catalog(cfg: CatalogToolConfig, launchers: LauncherFactory) -> list[BaseTool]:
        return build_catalog_tools(
            cfg, catalog.catalog_service_ref, StudioTools.page_prefix
        )

    @staticmethod
    def page_prefix() -> str:
        """Адрес страницы studio из конфига корневого контейнера; зовётся на вызов."""
        root = Container.require_root("page_prefix")
        config = root.resolved(runtime.get_runtime_config)
        if not isinstance(config, StudioAppConfig):
            msg = (
                "page_prefix expects StudioAppConfig from the runtime config provider, "
                f"got {type(config).__name__}"
            )
            raise RuntimeError(msg)

        return config.studio.page_prefix()
