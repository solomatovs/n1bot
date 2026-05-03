"""Entry-point boba-cli-vector-index. Pure runner поверх IndexPipeline."""

from __future__ import annotations

import logging
import sys
from typing import Any

from boba.cli.vector_index.config import VectorIndexConfig, VectorIndexSection
from boba.cli.vector_index.plugin_loader import (
    ChunkerPluginLoader,
    ReaderPluginLoader,
    SourcePluginLoader,
    StorePluginLoader,
)
from boba.config.app import AppConfig
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.section import ConfigSection
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.declaration import FieldPathMissingError
from boba.indexing import (
    Chunker,
    ChunkerId,
    CollectionInfo,
    IndexerExtensionContext,
    IndexingContext,
    IndexPipeline,
    PipelineId,
    ReaderDispatcher,
    Source,
    SourceId,
    Store,
    StoreId,
)
from boba.patterns import ConverterInputError

logger = logging.getLogger("boba.cli.vector_index")


def main() -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    boot = AppConfigBootstrap()
    boot.register_section(VectorIndexSection())
    boot.discover_extension_sections()
    boot.attach_sources(
        [
            CliSource(),
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(),
            TomlSource(),
        ]
    )

    try:
        app = boot.build()
        run_cfg = app.section(VectorIndexSection)
        _setup_logging(run_cfg.verbose)
        handler = _HANDLERS[run_cfg.action]
        return handler(run_cfg, app)
    except ConverterInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


_VERBOSE_INFO = 1
_VERBOSE_DEBUG = 2


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == _VERBOSE_INFO:
        level = logging.INFO
    elif verbose >= _VERBOSE_DEBUG:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _require(
    value: object,
    section: type[ConfigSection[Any]],
    field_name: str,
    action: str,
) -> None:
    """Per-action обязательность; бросает FieldPathMissingError."""
    if value not in (None, "", []):
        return
    del section
    raise FieldPathMissingError(
        f"action={action!r}: field {field_name!r} is required",
        field_name=field_name,
    )


def _handle_index(cfg: VectorIndexConfig, app: AppConfig) -> int:
    _require(cfg.collection, VectorIndexSection, "collection", "index")
    collection = cfg.collection or ""

    ctx = IndexerExtensionContext(config=app)
    pipeline = IndexPipeline(
        source=_build_source(ctx, cfg.source),
        reader=_build_reader_dispatcher(ctx),
        chunker=_build_chunker(ctx, cfg.chunker),
        store=_build_store(ctx, cfg.store),
    )
    icx = IndexingContext(
        pipeline_id=PipelineId(f"cli:{collection}"),
        collection=collection,
    )
    stats = pipeline.run(icx, description=cfg.description)
    print(
        f"collection={collection!r} "
        f"sources_processed={stats.sources_processed} "
        f"sources_skipped_unchanged={stats.sources_skipped_unchanged} "
        f"sources_failed={stats.sources_failed} "
        f"sections_emitted={stats.sections_emitted} "
        f"chunks_upserted={stats.chunks_upserted} "
        f"chunks_deleted={stats.chunks_deleted}"
    )
    return 0 if stats.sources_failed == 0 else 1


def _handle_list(cfg: VectorIndexConfig, app: AppConfig) -> int:
    del cfg
    ctx = IndexerExtensionContext(config=app)
    store = _build_store(ctx, app.section(VectorIndexSection).store)
    collections = list(store.list_collections())
    if not collections:
        print("(no collections)")
        return 0
    for c in collections:
        _print_collection_info(c)
    return 0


def _handle_delete(cfg: VectorIndexConfig, app: AppConfig) -> int:
    _require(cfg.collection, VectorIndexSection, "collection", "delete")
    collection = cfg.collection or ""

    if not cfg.confirm_skip:
        prompt = f"Delete collection {collection!r}? Type 'yes' to confirm: "
        if input(prompt).strip().lower() != "yes":
            print("aborted", file=sys.stderr)
            return 1
    ctx = IndexerExtensionContext(config=app)
    store = _build_store(ctx, cfg.store)
    store.delete_collection(collection)
    print(f"collection={collection!r} deleted")
    return 0


def _build_source(ctx: IndexerExtensionContext, source_id: str) -> Source:
    catalog = SourcePluginLoader(ctx).registry().build(ctx)
    sid = SourceId(source_id)
    if sid not in catalog:
        available = ", ".join(s.to_wire() for s in catalog)
        msg = (
            f"unknown source {source_id!r}. "
            f"available: {available or '(none — install boba-ext-*-source)'}"
        )
        raise ConverterInputError(msg)
    return catalog[sid]


def _build_reader_dispatcher(
    ctx: IndexerExtensionContext,
) -> ReaderDispatcher:
    return ReaderPluginLoader(ctx).registry().build()


def _build_chunker(ctx: IndexerExtensionContext, chunker_id: str) -> Chunker:
    catalog = ChunkerPluginLoader(ctx).registry().build(ctx)
    cid = ChunkerId(chunker_id)
    if cid not in catalog:
        available = ", ".join(c.to_wire() for c in catalog)
        msg = (
            f"unknown chunker {chunker_id!r}. "
            f"available: {available or '(none — install boba-ext-*-chunker)'}"
        )
        raise ConverterInputError(msg)
    return catalog[cid]


def _build_store(ctx: IndexerExtensionContext, store_id: str) -> Store:
    catalog = StorePluginLoader(ctx).registry().build(ctx)
    sid = StoreId(store_id)
    if sid not in catalog:
        available = ", ".join(s.to_wire() for s in catalog)
        msg = (
            f"unknown store {store_id!r}. "
            f"available: {available or '(none — install boba-ext-*-store)'}"
        )
        raise ConverterInputError(msg)
    return catalog[sid]


def _print_collection_info(c: CollectionInfo) -> None:
    desc = f" — {c.description}" if c.description else ""
    print(f"{c.name}\t{c.count} chunks{desc}")


def _handle_sync(cfg: VectorIndexConfig, app: AppConfig) -> int:
    """Удалить из Store чанки, source_id'ы которых отсутствуют у Source."""
    _require(cfg.collection, VectorIndexSection, "collection", "sync")
    collection = cfg.collection or ""

    ctx = IndexerExtensionContext(config=app)
    source = _build_source(ctx, cfg.source)
    store = _build_store(ctx, cfg.store)
    icx = IndexingContext(
        pipeline_id=PipelineId(f"cli-sync:{collection}"),
        collection=collection,
    )

    in_source = set(source.list_source_ids())
    in_store = set(store.list_source_ids(icx))
    orphans = sorted(in_store - in_source)

    deleted = 0
    for orphan in orphans:
        deleted += store.delete_by_source(icx, orphan)
    print(
        f"collection={collection!r} "
        f"in_source={len(in_source)} "
        f"in_store={len(in_store)} "
        f"orphans={len(orphans)} "
        f"chunks_deleted={deleted}"
    )
    return 0


_HANDLERS = {
    "index": _handle_index,
    "list": _handle_list,
    "delete": _handle_delete,
    "sync": _handle_sync,
}
