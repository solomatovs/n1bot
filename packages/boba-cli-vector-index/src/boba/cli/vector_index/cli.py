"""argparse-парсер и dispatch для команд boba-cli-vector-index.

Конфиг полностью идёт через ConfigSource-цепочку (Cli/Env/Toml — все
автономны). argparse тут только для --help; все значения — поля
:class:`VectorIndexSection`, читаемые через bundle. Имена флагов
вычисляются из ConfigKey'ев (см. cli_flag_name).

Доступные actions (значения поля ``vector_index.action``):

* ``index``  — почанковать и upsert файлы в коллекцию (paths +
  collection обязательны).
* ``list``   — показать существующие коллекции с количеством чанков.
* ``delete`` — удалить коллекцию целиком (collection обязателен;
  ``confirm_skip=true`` пропускает интерактивное подтверждение).

Точка входа: main. Также доступно как python -m boba.cli.vector_index.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from boba.cli.vector_index.config import (
    CliConfigError,
    VectorIndexConfig,
    VectorIndexSection,
    build_resolver,
    load_persist_path,
)
from boba.cli.vector_index.indexer import (
    IndexOptions,
    index_paths,
)
from boba.cli.vector_index.store import VectorStore
from boba.infra import ConfigFactory, ConfigLoader

logger = logging.getLogger("boba.cli.vector_index")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    # argv нужен только для --help/typo-validation. Источники внутри
    # build_resolver() читают свои каналы (sys.argv/env/TOML) сами.
    _build_parser().parse_known_args(argv)

    resolver = build_resolver()
    factory = ConfigFactory(resolver)
    factory.register(VectorIndexSection())
    factory.discover_extension_sections()

    try:
        bundle = ConfigLoader(factory).load_bundle()
        run_cfg = bundle.section(VectorIndexSection)
        persist_path = load_persist_path(resolver)
    except CliConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # top-level CLI error reporter
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    _setup_logging(run_cfg.verbose)

    handler = _HANDLERS.get(run_cfg.action)
    if handler is None:
        # OneOf-валидатор уже отверг бы невалидный action — этот
        # branch недостижим при корректной схеме, оставлен для
        # type-checker'а.
        print(f"error: unknown action {run_cfg.action!r}", file=sys.stderr)
        return 2
    try:
        return handler(persist_path, run_cfg)
    except CliConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    """argparse только для --help. Все значения — через source chain."""
    return argparse.ArgumentParser(
        prog="boba-cli-vector-index",
        description=(
            "Operator CLI: index/list/delete documents in the Boba "
            "vector knowledge base (ChromaDB). Все значения — поля "
            "VectorIndexSection, передаются через --vector-index-<field> "
            "flags (см. cli_flag_name) или env/TOML. Обязательно "
            "--vector-index-action; per-action входы (paths/collection) — "
            "по таблице --help секции."
        ),
    )


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


# ──────────────────────────────────────────────────────────────────────
# Action handlers
# ──────────────────────────────────────────────────────────────────────


def _require(value: object, name: str, action: str) -> None:
    if value in (None, "", []):
        raise CliConfigError(
            f"action={action!r}: {name} is required "
            f"(pass --vector-index-{name.replace('_', '-')}, "
            f"set BOBA_VECTOR_INDEX_{name.upper()}, или укажи "
            f"[vector_index] {name} в TOML)"
        )


def _handle_index(persist_path: str, cfg: VectorIndexConfig) -> int:
    _require(cfg.paths, "paths", "index")
    _require(cfg.collection, "collection", "index")
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
    return 0


def _handle_list(persist_path: str, cfg: VectorIndexConfig) -> int:
    del cfg
    store = VectorStore(persist_path)
    collections = store.list_collections()
    if not collections:
        print("(no collections)")
        return 0
    for c in collections:
        desc = f" — {c.description}" if c.description else ""
        print(f"{c.name}\t{c.count} chunks{desc}")
    return 0


def _handle_delete(persist_path: str, cfg: VectorIndexConfig) -> int:
    _require(cfg.collection, "collection", "delete")
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
