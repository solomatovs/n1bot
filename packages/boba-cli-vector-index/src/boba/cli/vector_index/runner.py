"""VectorIndexCli: CLI для action=index | print.

Поток:
  stage 1 — `_get_action()` строит mini-AppConfig с одной только
            `VectorIndexActionSection` и читает (action, pipeline);
  stage 2 — `_handle_index` собирает `AppConfigBootstrap` (свои секции +
            `discover_extension_sections()` + `PipelineSpec.section`
            выбранного плагина), вызывает `boot.build()` и зовёт
            `spec.build(app)` для сборки `IndexPipeline`;
            `_handle_print` собирает `PrintPipeline` поверх Store
            (без plugin-discovery — это admin-операция Store-уровня).

Sync/list/delete вырезаны: будут переделаны через отдельные pipeline-
абстракции, чтобы оркестрация жила вне CLI.
"""

from __future__ import annotations

import logging
import sys

from boba.chromadb_store import ChromadbPersistStore
from boba.cli.vector_index.config import (
    IndexCommandSection,
    PrintCommandSection,
    SearchCommandSection,
    VectorIndexActionConfig,
    VectorIndexActionSection,
    VectorIndexChromadbSection,
    VectorIndexCommonSection,
)
from boba.cli.vector_index.plugin_loader import (
    PipelinePluginError,
    PipelinePluginLoader,
)
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.config.source.toml import TomlFileSource, TomlSource
from boba.ext.chromadb_shared import (
    ChromadbSharedSection,
    make_embedding_function,
)
from boba.indexing import IndexingContext, IndexingError, PipelineId
from boba.patterns import ConverterInputError
from boba.print_pipeline import PrintPipeline
from boba.search_pipeline import SearchPipeline

__all__ = ["VectorIndexCli"]


class VectorIndexCli:
    """CLI для action=index."""

    def main(self) -> int:
        try:
            match self._get_action():
                case VectorIndexActionConfig(action="index", pipeline=pid) if pid:
                    return self._handle_index(pid)
                case VectorIndexActionConfig(action="print"):
                    return self._handle_print()
                case VectorIndexActionConfig(action="search"):
                    return self._handle_search()
                case other:
                    msg = f"unsupported action {other.action!r}"
                    raise ValueError(msg)
        except (
            ConverterInputError,
            IndexingError,
            PipelinePluginError,
            ValueError,
        ) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    def _get_action(self) -> VectorIndexActionConfig:
        boot = self._make_boot()
        boot.register_section(VectorIndexActionSection())
        return boot.build().section(VectorIndexActionSection)

    @staticmethod
    def _make_boot() -> AppConfigBootstrap:
        """`AppConfigBootstrap` с прикреплёнными source'ами; build делает caller."""
        boot = AppConfigBootstrap()
        boot.attach_sources(
            [
                CliSource(),
                EnvFileSource(),
                EnvSource(),
                TomlFileSource(),
                TomlSource(),
            ]
        )
        return boot

    @staticmethod
    def _setup_logging(verbose: int) -> None:
        levels = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
        logging.basicConfig(
            level=levels.get(verbose, logging.DEBUG),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def _handle_index(self, pipeline_id: str) -> int:
        boot = self._make_boot()
        boot.register_section(VectorIndexCommonSection())
        boot.register_section(VectorIndexChromadbSection())
        boot.register_section(IndexCommandSection())
        boot.discover_extension_sections()

        pid = PipelineId.from_wire(pipeline_id)
        spec = PipelinePluginLoader().registry().get(pid)
        boot.register_section(spec.section)
        app = boot.build()
        pipeline = spec.build(app)

        self._setup_logging(app.section(VectorIndexCommonSection).verbose)
        cfg = app.section(IndexCommandSection)
        icx = IndexingContext(
            pipeline_id=PipelineId(f"cli:{cfg.collection}"),
            collection=cfg.collection,
        )
        stats = pipeline.run(icx, description=cfg.description)
        print()
        print(
            f"collection={cfg.collection!r} pipeline={pid.to_wire()!r} "
            f"sources_processed={stats.sources_processed} "
            f"sources_failed={stats.sources_failed} "
            f"sources_skipped_unchanged={stats.sources_skipped_unchanged} "
            f"sections_emitted={stats.sections_emitted} "
            f"chunks_upserted={stats.chunks_upserted} "
            f"chunks_deleted={stats.chunks_deleted}"
        )
        return 0

    def _handle_print(self) -> int:
        boot = self._make_boot()
        boot.register_section(VectorIndexCommonSection())
        boot.register_section(VectorIndexChromadbSection())
        boot.register_section(ChromadbSharedSection())
        boot.register_section(PrintCommandSection())
        app = boot.build()

        self._setup_logging(app.section(VectorIndexCommonSection).verbose)
        cfg = app.section(PrintCommandSection)
        shared = app.section(VectorIndexChromadbSection)
        ef_cfg = app.section(ChromadbSharedSection)
        pipeline = PrintPipeline(
            store=ChromadbPersistStore(
                persist_path=shared.persist_path,
                embedding_function=make_embedding_function(ef_cfg),
            ),
            collection=cfg.collection,
            source_id=cfg.source_id,
            limit=cfg.limit,
            snippet_chars=cfg.snippet_chars,
        )
        stats = pipeline.run()
        print()
        print(
            f"collection={cfg.collection!r} "
            f"embedding_dim={stats.embedding_dim} "
            f"chunks_printed={stats.chunks_printed} "
            f"sources_seen={stats.sources_seen}"
        )
        return 0

    def _handle_search(self) -> int:
        boot = self._make_boot()
        boot.register_section(VectorIndexCommonSection())
        boot.register_section(VectorIndexChromadbSection())
        boot.register_section(ChromadbSharedSection())
        boot.register_section(SearchCommandSection())
        app = boot.build()

        self._setup_logging(app.section(VectorIndexCommonSection).verbose)
        cfg = app.section(SearchCommandSection)
        shared = app.section(VectorIndexChromadbSection)
        ef_cfg = app.section(ChromadbSharedSection)
        pipeline = SearchPipeline(
            store=ChromadbPersistStore(
                persist_path=shared.persist_path,
                embedding_function=make_embedding_function(ef_cfg),
            ),
            collection=cfg.collection,
            query=cfg.query,
            top_k=cfg.top_k,
            snippet_chars=cfg.snippet_chars,
        )
        stats = pipeline.run()
        print()
        print(
            f"collection={cfg.collection!r} "
            f"embedding_dim={stats.embedding_dim} "
            f"query={cfg.query!r} "
            f"hits_returned={stats.hits_returned}"
        )
        return 0
