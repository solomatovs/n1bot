"""boba-cli-vector-index: pure runner поверх pipeline-плагинов."""

from __future__ import annotations

import logging
import sys

from boba.chromadb_store import ChromadbPersistStore
from boba.cli.vector_index.config import VectorIndexConfig, VectorIndexSection
from boba.cli.vector_index.plugin_loader import PipelinePluginLoader
from boba.config.app import AppConfig
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.section import ConfigSection
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.declaration import FieldPathMissingError
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.indexing import (
    CollectionInfo,
    IndexerExtensionContext,
    IndexingContext,
    IndexPipeline,
    PipelineId,
    Store,
)
from boba.patterns import ConverterInputError

__all__ = ["main"]


def main() -> int:
    """Entry-point CLI."""
    boot = AppConfigBootstrap()
    boot.register_section(VectorIndexSection())
    boot.discover_extension_sections()
    boot.attach_sources(
        [CliSource(), EnvFileSource(), EnvSource(), TomlFileSource(), TomlSource()]
    )
    try:
        app = boot.build()
        run_cfg = app.section(VectorIndexSection)
        _setup_logging(run_cfg.verbose)
        handler = _HANDLERS[run_cfg.action]
        return handler(run_cfg, app)
    except (ConverterInputError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


# ---- index / sync: требуют pipeline-плагин ----

def _handle_index(cfg: VectorIndexConfig, app: AppConfig) -> int:
    _require(cfg.collection, VectorIndexSection, "collection", "index")
    _require(cfg.pipeline, VectorIndexSection, "pipeline", "index")
    pipeline = _resolve_pipeline(app, cfg.pipeline)
    icx = IndexingContext(
        pipeline_id=PipelineId(f"cli:{cfg.collection}"),
        collection=cfg.collection,
    )
    stats = pipeline.run(icx, description=cfg.description)
    print(
        f"collection={cfg.collection!r} pipeline={cfg.pipeline!r} "
        f"sources_processed={stats.sources_processed} "
        f"sources_failed={stats.sources_failed} "
        f"sources_skipped_unchanged={stats.sources_skipped_unchanged} "
        f"sections_emitted={stats.sections_emitted} "
        f"chunks_upserted={stats.chunks_upserted} "
        f"chunks_deleted={stats.chunks_deleted}"
    )
    return 0


def _handle_sync(cfg: VectorIndexConfig, app: AppConfig) -> int:
    """sync = diff between RequestSource canonical ids vs Store, удалить orphans."""
    _require(cfg.collection, VectorIndexSection, "collection", "sync")
    _require(cfg.pipeline, VectorIndexSection, "pipeline", "sync")
    pipeline = _resolve_pipeline(app, cfg.pipeline)
    icx = IndexingContext(
        pipeline_id=PipelineId(f"cli-sync:{cfg.collection}"),
        collection=cfg.collection,
    )
    rs = pipeline.request_source
    store = pipeline.store

    in_source = set(rs.list_source_ids(icx))
    in_store = set(store.list_source_ids(icx))
    orphans = sorted(in_store - in_source)
    deleted = sum(store.delete_by_source(icx, sid) for sid in orphans)
    print(
        f"collection={cfg.collection!r} "
        f"in_source={len(in_source)} in_store={len(in_store)} "
        f"orphans={len(orphans)} chunks_deleted={deleted}"
    )
    return 0


# ---- list / show / delete: только Store, pipeline не требуется ----

def _handle_list(cfg: VectorIndexConfig, app: AppConfig) -> int:
    del cfg
    store = _resolve_store(app)
    cols = list(store.list_collections())
    if not cols:
        print("(no collections)")
        return 0
    for c in cols:
        _print_collection_info(c)
    return 0


def _handle_show(cfg: VectorIndexConfig, app: AppConfig) -> int:
    _require(cfg.collection, VectorIndexSection, "collection", "show")
    store = _resolve_store(app)
    icx = IndexingContext(
        pipeline_id=PipelineId(f"cli-show:{cfg.collection}"),
        collection=cfg.collection,
    )
    rows = list(
        store.peek_chunks(
            icx,
            source_id=cfg.show_source_id,
            limit=cfg.show_limit,
            snippet_chars=cfg.show_snippet_chars,
        )
    )
    if not rows:
        scope = (
            f" source_id={cfg.show_source_id!r}" if cfg.show_source_id else ""
        )
        print(f"collection={cfg.collection!r}{scope}: (empty)")
        return 0
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r.source_id] = by_source.get(r.source_id, 0) + 1
    print(
        f"collection={cfg.collection!r} shown={len(rows)} "
        f"unique_sources={len(by_source)}"
    )
    print()
    for r in rows:
        title = (
            r.metadata.get("title") or r.metadata.get("heading_text") or ""
        )
        anchor = f"#{r.anchor}" if r.anchor else ""
        head = f"[{r.chunk_index:>3}] {r.source_id}{anchor}"
        if title:
            head += f"  ({title})"
        print(head)
        print(f"      {r.snippet}")
        print()
    return 0


def _handle_delete(cfg: VectorIndexConfig, app: AppConfig) -> int:
    _require(cfg.collection, VectorIndexSection, "collection", "delete")
    store = _resolve_store(app)
    if not cfg.confirm_skip:
        prompt = (
            f"Delete collection {cfg.collection!r}? Type 'yes' to confirm: "
        )
        if input(prompt).strip().lower() != "yes":
            print("aborted", file=sys.stderr)
            return 1
    store.delete_collection(cfg.collection)
    print(f"collection={cfg.collection!r} deleted")
    return 0


# ---- helpers ----

def _resolve_pipeline(app: AppConfig, pipeline_id: str) -> IndexPipeline:
    """Lazy: ищем factory по id и строим только этот pipeline."""
    ctx = IndexerExtensionContext(config=app)
    registry = PipelinePluginLoader(ctx).registry()
    pid = PipelineId(pipeline_id)
    factory = registry.get(pid)
    if factory is None:
        available = ", ".join(p.to_wire() for p in registry.factories())
        msg = (
            f"unknown pipeline {pipeline_id!r}. "
            f"available: {available or '(none — install boba-ext-*-pipeline)'}"
        )
        raise ConverterInputError(msg)
    return factory.produce(ctx)


def _resolve_store(app: AppConfig) -> Store:
    """list/show/delete — admin-операции над Store; pipeline не нужен.

    Store создаётся напрямую из `[ext.chromadb] persist_path`. Это значит
    все pipeline'ы в системе используют **общий** ChromaDB; admin-команды
    смотрят в него независимо от того, какие pipeline-плагины установлены.
    """
    shared = app.section(ChromadbSharedSection)
    if not shared.persist_path:
        msg = (
            "[ext.chromadb] persist_path пуст — задайте в TOML или env "
            "BOBA_EXT__CHROMADB__PERSIST_PATH."
        )
        raise ConverterInputError(msg)
    return ChromadbPersistStore(persist_path=shared.persist_path)


def _print_collection_info(c: CollectionInfo) -> None:
    desc = f" — {c.description}" if c.description else ""
    print(f"{c.name}\t{c.count} chunks{desc}")


def _require(
    value: object,
    section_cls: type[ConfigSection],
    field_name: str,
    action_label: str,
) -> None:
    if not value:
        raise FieldPathMissingError(
            f"field {field_name!r} обязательно для action={action_label!r} "
            f"(секция {section_cls.__name__})",
            field_name=field_name,
        )


def _setup_logging(verbose: int) -> None:
    levels = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
    level = levels.get(verbose, logging.DEBUG)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


_HANDLERS = {
    "index": _handle_index,
    "list": _handle_list,
    "delete": _handle_delete,
    "sync": _handle_sync,
    "show": _handle_show,
}
