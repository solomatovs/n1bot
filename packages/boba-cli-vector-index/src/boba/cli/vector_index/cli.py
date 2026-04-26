"""argparse-парсер и dispatch для команд boba-cli-vector-index.

Доступные команды (v0.1):

* index <paths>... — почанковать и upsert файлы в коллекцию.
* list — показать существующие коллекции с количеством чанков.
* delete --collection <name> — удалить коллекцию целиком.

Точка входа: main. Также доступно как
python -m boba.cli.vector_index.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from boba.cli.vector_index.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
)
from boba.cli.vector_index.config import CliConfig, CliConfigError
from boba.cli.vector_index.indexer import (
    IndexOptions,
    index_paths,
)
from boba.cli.vector_index.store import VectorStore
from boba.config.cli import CliFlag, add_to_parser
from boba.domain.core.config import ConfigKey

logger = logging.getLogger("boba.cli.vector_index")

# Декларативный список config-CLI-флагов: пакет boba.config.cli использует
# его и для add_to_parser, и для from_namespace (внутри CliConfig.resolve).
# Per-command флаги (--collection, --chunk-size, ...) — это входы команды,
# не конфиг; они декларируются прямо на subparser'ах.
_FLAGS: tuple[CliFlag, ...] = (
    CliFlag(
        ConfigKey("ext", "chromadb", "persist_path"),
        help=(
            "ChromaDB persist directory (overrides "
            "BOBA_EXT_CHROMADB_PERSIST_PATH / [ext.chromadb] persist_path)."
        ),
    ),
)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry-point. Возвращает exit-code (0 = успех)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    try:
        cfg = CliConfig.resolve(args=args, flags=_FLAGS)
    except CliConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    handler = _HANDLERS.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2
    return handler(cfg, args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boba-cli-vector-index",
        description=(
            "Operator CLI: index/list/delete documents in the Boba "
            "vector knowledge base (ChromaDB)."
        ),
    )
    add_to_parser(parser, _FLAGS)
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v: INFO, -vv: DEBUG)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Index documents into a collection")
    p_index.add_argument(
        "paths",
        nargs="+",
        help="Files or directories. Directories are walked recursively.",
    )
    p_index.add_argument(
        "--collection",
        required=True,
        help="Target collection name (created if missing).",
    )
    p_index.add_argument(
        "--description",
        default=None,
        help=(
            "Collection description (visible to the agent via "
            "kb_list_collections). Applied only on collection creation; "
            "existing collections are not re-described."
        ),
    )
    p_index.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Character chunk size (default {DEFAULT_CHUNK_SIZE}).",
    )
    p_index.add_argument(
        "--chunk-overlap",
        type=int,
        default=DEFAULT_CHUNK_OVERLAP,
        help=f"Character overlap between chunks (default {DEFAULT_CHUNK_OVERLAP}).",
    )

    sub.add_parser("list", help="List collections with chunk counts")

    p_delete = sub.add_parser("delete", help="Delete a collection")
    p_delete.add_argument(
        "--collection", required=True, help="Collection name to delete."
    )
    p_delete.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation.",
    )
    return parser


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


# --- Handlers ----------------------------------------------------------------


def _handle_index(cfg: CliConfig, args: argparse.Namespace) -> int:
    store = VectorStore(cfg.persist_path)
    options = IndexOptions(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )
    stats = index_paths(
        store,
        collection_name=args.collection,
        paths=list(args.paths),
        description=args.description,
        options=options,
    )
    print(
        f"collection={args.collection!r} "
        f"files_indexed={stats.files_indexed} "
        f"files_skipped={stats.files_skipped} "
        f"chunks_upserted={stats.chunks_upserted} "
        f"chunks_deleted={stats.chunks_deleted}"
    )
    return 0


def _handle_list(cfg: CliConfig, args: argparse.Namespace) -> int:
    del args
    store = VectorStore(cfg.persist_path)
    collections = store.list_collections()
    if not collections:
        print("(no collections)")
        return 0
    for c in collections:
        desc = f" — {c.description}" if c.description else ""
        print(f"{c.name}\t{c.count} chunks{desc}")
    return 0


def _handle_delete(cfg: CliConfig, args: argparse.Namespace) -> int:
    if not args.yes:
        prompt = f"Delete collection {args.collection!r}? Type 'yes' to confirm: "
        if input(prompt).strip().lower() != "yes":
            print("aborted", file=sys.stderr)
            return 1
    store = VectorStore(cfg.persist_path)
    store.delete_collection(args.collection)
    print(f"collection={args.collection!r} deleted")
    return 0


_HANDLERS = {
    "index": _handle_index,
    "list": _handle_list,
    "delete": _handle_delete,
}
