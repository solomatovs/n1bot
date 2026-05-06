"""PipelinePluginLoader: discovery `boba.indexing.pipelines` через entry-points.

Каждый плагин экспортирует константу `PipelineSpec` (`section + build`).
Имя entry-point равно `PipelineId` (например `ext.fs_markdown`).
"""

from __future__ import annotations

import importlib.metadata
import logging

from boba.indexing import PipelineRegistry, PipelineSpec
from boba.processing import PipelineId

__all__ = [
    "PIPELINES_ENTRY_POINT_GROUP",
    "PipelinePluginError",
    "PipelinePluginLoadError",
    "PipelinePluginLoader",
    "PipelinePluginRegisterError",
]

logger = logging.getLogger(__name__)

PIPELINES_ENTRY_POINT_GROUP = "boba.indexing.pipelines"


class PipelinePluginError(Exception):
    """База ошибок discovery pipeline-плагинов."""


class PipelinePluginLoadError(PipelinePluginError):
    """Не удалось загрузить entry-point pipeline-плагина."""

    def __init__(self, ep_name: str, reason: str) -> None:
        super().__init__(f"pipeline-plugin {ep_name!r} load failed: {reason}")


class PipelinePluginRegisterError(PipelinePluginError):
    """Не удалось зарегистрировать pipeline-плагин."""

    def __init__(self, ep_name: str, reason: str) -> None:
        super().__init__(f"pipeline-plugin {ep_name!r} register failed: {reason}")


class PipelinePluginLoader:
    """Discovery `PipelineSpec` через `boba.indexing.pipelines`."""

    def __init__(self) -> None:
        self._registry = PipelineRegistry()
        self._discover()

    def registry(self) -> PipelineRegistry:
        return self._registry

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(
            group=PIPELINES_ENTRY_POINT_GROUP,
        ):
            try:
                self._load_and_register(ep)
            except PipelinePluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(self, ep: importlib.metadata.EntryPoint) -> None:
        try:
            obj = ep.load()
        except Exception as e:
            raise PipelinePluginLoadError(
                ep.name, f"{type(e).__name__}: {e}",
            ) from e
        if not isinstance(obj, PipelineSpec):
            raise PipelinePluginRegisterError(
                ep.name, f"expected PipelineSpec, got {type(obj).__name__}",
            )
        self._registry.register(PipelineId(ep.name), obj)
