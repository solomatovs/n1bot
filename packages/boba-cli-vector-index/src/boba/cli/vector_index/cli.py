"""Entry-point boba-cli-vector-index. Actions: index/list/delete."""

from __future__ import annotations

import logging
import sys
from typing import Any

from boba_next.config import AppConfigBootstrap, ConfigSection
from boba_next.declaration import FieldPathMissingError

from boba.cli.vector_index.config import (
    ChromadbPersistSection,
    VectorIndexConfig,
    VectorIndexSection,
)
from boba.cli.vector_index.indexer import (
    IndexOptions,
    index_paths,
)
from boba.cli.vector_index.store import CollectionSummary, VectorStore
from boba.config.cli import CliSource
from boba.config.env import EnvFileSource, EnvSource
from boba.config.toml import TomlFileSource, TomlSource
from boba.patterns import ConverterInputError

logger = logging.getLogger("boba.cli.vector_index")


def main() -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    boot = AppConfigBootstrap()
    boot.register_section(VectorIndexSection())
    boot.register_section(ChromadbPersistSection())
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
        persist_path = app.section(ChromadbPersistSection).persist_path
        _setup_logging(run_cfg.verbose)
        handler = _HANDLERS[run_cfg.action]
        return handler(persist_path, run_cfg)
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
    del section  # signature kept for API; FieldPathMissingError несёт field_name
    raise FieldPathMissingError(
        f"action={action!r}: field {field_name!r} is required",
        field_name=field_name,
    )


def _handle_index(persist_path: str, cfg: VectorIndexConfig) -> int:
    _require(cfg.paths, VectorIndexSection, "paths", "index")
    _require(cfg.collection, VectorIndexSection, "collection", "index")
    paths = cfg.paths or []
    collection = cfg.collection or ""

    store = VectorStore(persist_path)
    options = IndexOptions(
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
    )
    stats = index_paths(
        store,
        collection_name=collection,
        paths=list(paths),
        description=cfg.description,
        options=options,
    )
    print(
        f"collection={collection!r} "
        f"files_indexed={stats.files_indexed} "
        f"files_skipped={stats.files_skipped} "
        f"chunks_upserted={stats.chunks_upserted} "
        f"chunks_deleted={stats.chunks_deleted}"
    )
    summary = store.get_collection_summary(collection)
    _print_collection_summary(summary)
    return 0


def _handle_list(persist_path: str, cfg: VectorIndexConfig) -> int:
    del cfg
    store = VectorStore(persist_path)
    collections = store.list_collections()
    if not collections:
        print("(no collections)")
        return 0
    for c in collections:
        _print_collection_summary(c)
    return 0


def _print_collection_summary(c: CollectionSummary) -> None:
    desc = f" — {c.description}" if c.description else ""
    print(f"{c.name}\t{c.count} chunks{desc}")


def _handle_delete(persist_path: str, cfg: VectorIndexConfig) -> int:
    _require(cfg.collection, VectorIndexSection, "collection", "delete")
    collection = cfg.collection or ""

    if not cfg.confirm_skip:
        prompt = f"Delete collection {collection!r}? Type 'yes' to confirm: "
        if input(prompt).strip().lower() != "yes":
            print("aborted", file=sys.stderr)
            return 1
    store = VectorStore(persist_path)
    store.delete_collection(collection)
    print(f"collection={collection!r} deleted")
    return 0


_HANDLERS = {
    "index": _handle_index,
    "list": _handle_list,
    "delete": _handle_delete,
}
