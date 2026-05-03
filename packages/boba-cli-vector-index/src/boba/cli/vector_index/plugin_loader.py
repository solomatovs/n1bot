"""Entry-points loader для Reader/Source-плагинов индексатора."""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable, Iterable
from typing import cast

from boba.config.app import ConfigError
from boba.indexing import (
    ChunkerFactory,
    ChunkerRegistry,
    IndexerExtensionContext,
    Reader,
    ReaderRegistry,
    SourceFactory,
    SourceRegistry,
    StoreFactory,
    StoreRegistry,
)

__all__ = [
    "CHUNKERS_ENTRY_POINT_GROUP",
    "READERS_ENTRY_POINT_GROUP",
    "SOURCES_ENTRY_POINT_GROUP",
    "STORES_ENTRY_POINT_GROUP",
    "ChunkerPluginLoader",
    "PluginError",
    "PluginLoadError",
    "PluginRegisterError",
    "ReaderPluginLoader",
    "SourcePluginLoader",
    "StorePluginLoader",
]

logger = logging.getLogger(__name__)

READERS_ENTRY_POINT_GROUP = "boba.indexing.readers"
SOURCES_ENTRY_POINT_GROUP = "boba.indexing.sources"
CHUNKERS_ENTRY_POINT_GROUP = "boba.indexing.chunkers"
STORES_ENTRY_POINT_GROUP = "boba.indexing.stores"

RegisterReadersFn = Callable[[IndexerExtensionContext], Iterable[Reader]]
RegisterSourcesFn = Callable[[IndexerExtensionContext], Iterable[SourceFactory]]
RegisterChunkersFn = Callable[[IndexerExtensionContext], Iterable[ChunkerFactory]]
RegisterStoresFn = Callable[[IndexerExtensionContext], Iterable[StoreFactory]]


class PluginError(Exception):
    """База ошибок indexing-plugin инфры; несёт group + entry_point_name."""

    def __init__(self, group: str, entry_point_name: str, message: str) -> None:
        super().__init__(f"{group} plugin {entry_point_name!r}: {message}")
        self.group = group
        self.entry_point_name = entry_point_name


class PluginLoadError(PluginError):
    """ep.load() упал или target не callable."""


class PluginRegisterError(PluginError):
    """register_*(ctx) бросил или вернул некорректное."""


class ReaderPluginLoader:
    """Discovery Reader-плагинов через `boba.indexing.readers`.

    Плагин экспортирует `register_readers(ctx) -> Iterable[Reader]`.
    Ошибка одного плагина не валит остальных — логгируется и пропускается.
    """

    def __init__(self, ctx: IndexerExtensionContext) -> None:
        self._ctx = ctx
        self._registry = ReaderRegistry()
        self._discover()

    def registry(self) -> ReaderRegistry:
        return self._registry

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(group=READERS_ENTRY_POINT_GROUP):
            try:
                self._load_and_register(ep)
            except PluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(self, ep: importlib.metadata.EntryPoint) -> None:
        register = _resolve_register(ep, READERS_ENTRY_POINT_GROUP)
        register_fn = cast("RegisterReadersFn", register)
        try:
            for reader in register_fn(self._ctx):
                self._registry.register_reader(reader)
        except ConfigError:
            raise
        except Exception as e:
            raise PluginRegisterError(
                READERS_ENTRY_POINT_GROUP,
                ep.name,
                f"register_readers(ctx) failed: {type(e).__name__}: {e}",
            ) from e


class SourcePluginLoader:
    """Discovery SourceFactory-плагинов через `boba.indexing.sources`.

    Плагин экспортирует `register_sources(ctx) -> Iterable[SourceFactory]`.
    """

    def __init__(self, ctx: IndexerExtensionContext) -> None:
        self._ctx = ctx
        self._registry = SourceRegistry()
        self._discover()

    def registry(self) -> SourceRegistry:
        return self._registry

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(group=SOURCES_ENTRY_POINT_GROUP):
            try:
                self._load_and_register(ep)
            except PluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(self, ep: importlib.metadata.EntryPoint) -> None:
        register = _resolve_register(ep, SOURCES_ENTRY_POINT_GROUP)
        register_fn = cast("RegisterSourcesFn", register)
        try:
            for factory in register_fn(self._ctx):
                self._registry.register_factory(factory)
        except ConfigError:
            raise
        except Exception as e:
            raise PluginRegisterError(
                SOURCES_ENTRY_POINT_GROUP,
                ep.name,
                f"register_sources(ctx) failed: {type(e).__name__}: {e}",
            ) from e


class ChunkerPluginLoader:
    """Discovery ChunkerFactory-плагинов через `boba.indexing.chunkers`.

    Плагин экспортирует `register_chunkers(ctx) -> Iterable[ChunkerFactory]`.
    """

    def __init__(self, ctx: IndexerExtensionContext) -> None:
        self._ctx = ctx
        self._registry = ChunkerRegistry()
        self._discover()

    def registry(self) -> ChunkerRegistry:
        return self._registry

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(group=CHUNKERS_ENTRY_POINT_GROUP):
            try:
                self._load_and_register(ep)
            except PluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(self, ep: importlib.metadata.EntryPoint) -> None:
        register = _resolve_register(ep, CHUNKERS_ENTRY_POINT_GROUP)
        register_fn = cast("RegisterChunkersFn", register)
        try:
            for factory in register_fn(self._ctx):
                self._registry.register_factory(factory)
        except ConfigError:
            raise
        except Exception as e:
            raise PluginRegisterError(
                CHUNKERS_ENTRY_POINT_GROUP,
                ep.name,
                f"register_chunkers(ctx) failed: {type(e).__name__}: {e}",
            ) from e


class StorePluginLoader:
    """Discovery StoreFactory-плагинов через `boba.indexing.stores`.

    Плагин экспортирует `register_stores(ctx) -> Iterable[StoreFactory]`.
    """

    def __init__(self, ctx: IndexerExtensionContext) -> None:
        self._ctx = ctx
        self._registry = StoreRegistry()
        self._discover()

    def registry(self) -> StoreRegistry:
        return self._registry

    def _discover(self) -> None:
        for ep in importlib.metadata.entry_points(group=STORES_ENTRY_POINT_GROUP):
            try:
                self._load_and_register(ep)
            except PluginError as e:
                logger.warning("%s; skipped", e)

    def _load_and_register(self, ep: importlib.metadata.EntryPoint) -> None:
        register = _resolve_register(ep, STORES_ENTRY_POINT_GROUP)
        register_fn = cast("RegisterStoresFn", register)
        try:
            for factory in register_fn(self._ctx):
                self._registry.register_factory(factory)
        except ConfigError:
            raise
        except Exception as e:
            raise PluginRegisterError(
                STORES_ENTRY_POINT_GROUP,
                ep.name,
                f"register_stores(ctx) failed: {type(e).__name__}: {e}",
            ) from e


def _resolve_register(ep: importlib.metadata.EntryPoint, group: str) -> object:
    """ep.load() + проверка callable; иначе PluginLoadError."""
    try:
        obj = ep.load()
    except Exception as e:
        raise PluginLoadError(
            group,
            ep.name,
            f"entry-point load failed: {type(e).__name__}: {e}",
        ) from e
    if not callable(obj):
        raise PluginLoadError(
            group,
            ep.name,
            f"entry-point target is not callable: {type(obj).__name__}",
        )
    return obj
