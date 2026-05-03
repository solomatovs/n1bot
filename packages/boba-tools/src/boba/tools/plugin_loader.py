"""Entry-points loader для tool-плагинов (группа boba.tools)."""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

from boba.config.app import AppConfig, ConfigError
from boba.patterns import Always, Specification
from boba.tools.registry import ToolFactory, ToolSource, ToolsService
from boba.tools.tool import Tool

__all__ = [
    "ENTRY_POINTS_GROUP",
    "ExtensionContext",
    "ToolPluginError",
    "ToolPluginLoadError",
    "ToolPluginLoader",
    "ToolPluginRegisterError",
]

logger = logging.getLogger(__name__)


ENTRY_POINTS_GROUP = "boba.tools"

RegisterToolsFn = Callable[["ExtensionContext"], Iterable[ToolSource]]


class ToolPluginError(Exception):
    """Базовая ошибка tool-plugin инфры; несёт entry_point_name."""

    def __init__(self, entry_point_name: str, message: str) -> None:
        super().__init__(f"tool plugin {entry_point_name!r}: {message}")
        self.entry_point_name = entry_point_name


class ToolPluginLoadError(ToolPluginError):
    """Ошибка загрузки entry-point: ep.load() упал или вернул не callable."""


class ToolPluginRegisterError(ToolPluginError):
    """Ошибка инстанцирования: register_tools(ctx) бросил или вернул некорректное."""


@dataclass(frozen=True)
class ExtensionContext:
    """Контракт окружения для register_tools(ctx)."""

    config: AppConfig


class ToolPluginLoader:
    """Discovery tool-плагинов через entry-points boba.tools и сборка ToolsService."""

    def __init__(
        self,
        ctx: ExtensionContext,
        tool_spec: Specification[Tool[Any]] | None = None,
    ) -> None:
        self._ctx = ctx
        self._tool_spec: Specification[Tool[Any]] = (
            tool_spec if tool_spec is not None else Always[Tool[Any]]()
        )
        self._tool_sources: list[ToolSource] = []
        self._discover()
        self._tools_service = self._build_tools_service()

    def tools_service(self) -> ToolsService:
        """Закэшированный ToolsService со всеми зарегистрированными entry-point'ами."""
        return self._tools_service

    def _build_tools_service(self) -> ToolsService:
        factory = ToolFactory()
        for source in self._tool_sources:
            factory.register(source)
        service = ToolsService(factory)
        service.rebuild_catalog()
        service.filter(self._tool_spec)
        return service

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(group=ENTRY_POINTS_GROUP):
            try:
                self._load_and_register(ep)
            except ToolPluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(self, ep: importlib.metadata.EntryPoint) -> None:
        try:
            obj = ep.load()
        except Exception as e:
            raise ToolPluginLoadError(
                ep.name, f"entry-point load failed: {type(e).__name__}: {e}"
            ) from e
        if not callable(obj):
            raise ToolPluginLoadError(
                ep.name,
                f"entry-point target is not callable: {type(obj).__name__}",
            )
        register = cast("RegisterToolsFn", obj)
        try:
            for source in register(self._ctx):
                self._tool_sources.append(source)
        except ConfigError:
            raise
        except Exception as e:
            raise ToolPluginRegisterError(
                ep.name,
                f"register_tools(ctx) failed: {type(e).__name__}: {e}",
            ) from e
