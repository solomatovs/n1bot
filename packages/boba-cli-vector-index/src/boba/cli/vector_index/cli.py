"""boba-cli-vector-index: pure runner поверх pipeline-плагинов."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from boba.chromadb_store import ChromadbPersistStore
from boba.cli.vector_index.config import (
    DeleteCommandSection,
    IndexCommandSection,
    ShowCommandSection,
    SyncCommandSection,
    VectorIndexActionSection,
    VectorIndexChromadbSection,
    VectorIndexCommonSection,
    command_section_for,
)
from boba.cli.vector_index.plugin_loader import PipelinePluginLoader
from boba.config.app import AppConfig
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.indexing import (
    CollectionInfo,
    IndexerExtensionContext,
    IndexingContext,
    IndexPipeline,
    PipelineId,
    Store,
)
from boba.patterns import ConverterInputError

__all__ = ["VectorIndexCli", "main"]


class VectorIndexCli:
    """CLI для управления векторными индексами.

    Принимает уже собранный AppConfig (после two-stage bootstrap'а) и action.
    Каждый handler читает свой CommandSection с required-валидацией.
    """

    def __init__(self, app: AppConfig, action: str) -> None:
        self._app = app
        self._action = action
        self._common = app.section(VectorIndexCommonSection)
        self._handlers: dict[str, Callable[[], int]] = {
            "index": self._handle_index,
            "list": self._handle_list,
            "delete": self._handle_delete,
            "sync": self._handle_sync,
            "show": self._handle_show,
        }

    def setup_logging(self) -> None:
        levels = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
        level = levels.get(self._common.verbose, logging.DEBUG)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def run(self) -> int:
        return self._handlers[self._action]()

    def _handle_index(self) -> int:
        cfg = self._app.section(IndexCommandSection)
        pipeline = self._resolve_pipeline(cfg.pipeline)
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

    def _handle_sync(self) -> int:
        cfg = self._app.section(SyncCommandSection)
        pipeline = self._resolve_pipeline(cfg.pipeline)
        icx = IndexingContext(
            pipeline_id=PipelineId(f"cli-sync:{cfg.collection}"),
            collection=cfg.collection,
        )
        in_source = set(pipeline.request_source.list_source_ids(icx))
        in_store = set(pipeline.store.list_source_ids(icx))
        orphans = sorted(in_store - in_source)
        deleted = sum(
            pipeline.store.delete_by_source(icx, sid) for sid in orphans
        )
        print(
            f"collection={cfg.collection!r} "
            f"in_source={len(in_source)} in_store={len(in_store)} "
            f"orphans={len(orphans)} chunks_deleted={deleted}"
        )
        return 0

    def _handle_list(self) -> int:
        store = self._resolve_store()
        cols = list(store.list_collections())
        if not cols:
            print("(no collections)")
            return 0
        for c in cols:
            self._print_collection_info(c)
        return 0

    def _handle_show(self) -> int:
        cfg = self._app.section(ShowCommandSection)
        store = self._resolve_store()
        icx = IndexingContext(
            pipeline_id=PipelineId(f"cli-show:{cfg.collection}"),
            collection=cfg.collection,
        )
        rows = list(
            store.peek_chunks(
                icx,
                source_id=cfg.source_id or None,
                limit=cfg.limit,
                snippet_chars=cfg.snippet_chars,
            )
        )
        if not rows:
            scope = f" source_id={cfg.source_id!r}" if cfg.source_id else ""
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
                r.metadata.get("title")
                or r.metadata.get("heading_text")
                or ""
            )
            anchor = f"#{r.anchor}" if r.anchor else ""
            head = f"[{r.chunk_index:>3}] {r.source_id}{anchor}"
            if title:
                head += f"  ({title})"
            print(head)
            print(f"      {r.snippet}")
            print()
        return 0

    def _handle_delete(self) -> int:
        cfg = self._app.section(DeleteCommandSection)
        store = self._resolve_store()
        store.delete_collection(cfg.collection)
        print(f"collection={cfg.collection!r} deleted")
        return 0

    def _resolve_pipeline(self, pipeline_id: str) -> IndexPipeline:
        ctx = IndexerExtensionContext(config=self._app)
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

    def _resolve_store(self) -> Store:
        cfg = self._app.section(VectorIndexChromadbSection)
        return ChromadbPersistStore(persist_path=cfg.persist_path)

    @staticmethod
    def _print_collection_info(c: CollectionInfo) -> None:
        desc = f" — {c.description}" if c.description else ""
        print(f"{c.name}\t{c.count} chunks{desc}")


def _attach_sources(boot: AppConfigBootstrap) -> None:
    boot.attach_sources(
        [CliSource(), EnvFileSource(), EnvSource(), TomlFileSource(), TomlSource()]
    )


def main() -> int:
    """Two-stage bootstrap: stage-1 — узнать action; stage-2 — собрать всё нужное."""
    try:
        # stage 1: только action — для дискриминации
        boot1 = AppConfigBootstrap()
        boot1.register_section(VectorIndexActionSection())
        _attach_sources(boot1)
        action = boot1.build().section(VectorIndexActionSection).action

        # stage 2: action-specific CommandSection + общее + extensions
        boot2 = AppConfigBootstrap()
        boot2.register_section(VectorIndexActionSection())
        boot2.register_section(VectorIndexCommonSection())
        boot2.register_section(VectorIndexChromadbSection())
        cmd = command_section_for(action)
        if cmd is not None:
            boot2.register_section(cmd)
        boot2.discover_extension_sections()
        _attach_sources(boot2)
        app = boot2.build()

        cli = VectorIndexCli(app, action)
        cli.setup_logging()
        return cli.run()
    except (ConverterInputError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
