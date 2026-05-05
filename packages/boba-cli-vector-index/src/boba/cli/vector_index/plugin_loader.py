"""PipelinePluginLoader: discovery `boba.indexing.pipelines` через entry-points."""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable, Iterable
from typing import cast

from boba.config.app import ConfigError
from boba.indexing import (
    IndexerExtensionContext,
    PipelineFactory,
    PipelineRegistry,
)

__all__ = [
    "PIPELINES_ENTRY_POINT_GROUP",
    "PipelinePluginError",
    "PipelinePluginLoadError",
    "PipelinePluginLoader",
    "PipelinePluginRegisterError",
]

logger = logging.getLogger(__name__)

PIPELINES_ENTRY_POINT_GROUP = "boba.indexing.pipelines"

RegisterPipelinesFn = Callable[
    [IndexerExtensionContext], Iterable[PipelineFactory]
]


class PipelinePluginError(Exception):
    """База ошибок discovery pipeline-плагинов."""


class PipelinePluginLoadError(PipelinePluginError):
    """Не удалось загрузить entry-point pipeline-плагина."""

    def __init__(self, ep_name: str, reason: str) -> None:
        super().__init__(
            f"pipeline-plugin {ep_name!r} load failed: {reason}"
        )


class PipelinePluginRegisterError(PipelinePluginError):
    """Не удалось зарегистрировать pipeline-плагин в реестре."""

    def __init__(self, ep_name: str, reason: str) -> None:
        super().__init__(
            f"pipeline-plugin {ep_name!r} register failed: {reason}"
        )


class PipelinePluginLoader:
    """Discovery PipelineFactory'ев через `boba.indexing.pipelines`.

    Каждый плагин экспортирует `register_pipelines(ctx) -> Iterable[PipelineFactory]`.
    Loader читает entry-points, вызывает register-функцию, складывает фабрики
    в `PipelineRegistry`.
    """

    def __init__(self, ctx: IndexerExtensionContext) -> None:
        self._ctx = ctx
        self._registry = PipelineRegistry()
        self._discover()

    def registry(self) -> PipelineRegistry:
        return self._registry

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(
            group=PIPELINES_ENTRY_POINT_GROUP
        ):
            try:
                self._load_and_register(ep)
            except PipelinePluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(
        self, ep: importlib.metadata.EntryPoint
    ) -> None:
        try:
            register = ep.load()
        except Exception as e:
            raise PipelinePluginLoadError(
                ep.name, f"{type(e).__name__}: {e}"
            ) from e
        register_fn = cast("RegisterPipelinesFn", register)
        try:
            for factory in register_fn(self._ctx):
                self._registry.register_factory(factory)
        except ConfigError:
            raise
        except Exception as e:
            raise PipelinePluginRegisterError(
                ep.name, f"{type(e).__name__}: {e}"
            ) from e
