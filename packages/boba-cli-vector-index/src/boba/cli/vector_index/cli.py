"""Entry-point boba-cli-vector-index. Actions: index/list/delete."""

from __future__ import annotations

import logging
import sys
from typing import Any

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
from boba.config.toml import CONFIG_PATH_ENV, TomlFileSource, TomlSource
from boba.domain.core.config import ConfigKey, ConfigSection
from boba.domain.core.declaration import FieldMissingError
from boba.domain.core.patterns import ConverterInputError
from boba.infra import ConfigFactory

logger = logging.getLogger("boba.cli.vector_index")


def main() -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    factory = ConfigFactory()
    factory.register(VectorIndexSection())
    factory.register(ChromadbPersistSection())
    factory.discover_extension_sections()
    factory.attach_sources(
        [
            CliSource(),
            EnvFileSource(),
            EnvSource(extra_known={CONFIG_PATH_ENV}),
            TomlFileSource(),
            TomlSource(),
        ]
    )

    try:
        bundle = factory.build()
        run_cfg = bundle.section(VectorIndexSection)
        persist_path = bundle.section(ChromadbPersistSection).persist_path
        _setup_logging(run_cfg.verbose)
        handler = _HANDLERS[run_cfg.action]
        return handler(persist_path, run_cfg)
    except ConverterInputError as e:
        print(f"error: {factory.format_config_error(e)}", file=sys.stderr)
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
    """Per-action обязательность; бросает FieldMissingError."""
    if value not in (None, "", []):
        return
    spec = next(f for f in section.schema.fields if f.name == field_name)
    raise FieldMissingError(
        f"action={action!r}: field {field_name!r} is required",
        field=spec,
        key=ConfigKey(*section.namespace, field_name),
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
