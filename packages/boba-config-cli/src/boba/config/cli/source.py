"""CLI-arguments ConfigSource-реализация.

Алгоритм мапинга ConfigKey → CLI-flag (зеркальный env_name/toml_path):

    "--" + "-".join(p.replace("_", "-") for p in key.parts)

Например:

* ConfigKey("agent_run","model")              → --agent-run-model
* ConfigKey("agent_run","max_tokens")         → --agent-run-max-tokens
* ConfigKey("ext","chromadb","persist_path")  → --ext-chromadb-persist-path

Имя флага — это deterministic функция от ключа, как BOBA_AGENT_RUN_MODEL
для env. Никаких алиасов, никаких операторских имён.

Каждый CLI декларирует список CliFlag (только key + опционально
action/help); пакет добавляет add_argument через add_to_parser() и
собирает CliArgsSource из Namespace через from_namespace(). Обычно
идёт первым в ChainedConfigResolver. None и пустые строки фильтруются
конструктором CliArgsSource.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from boba.domain.core.config import ConfigKey, ConfigSource

__all__ = [
    "FLAG_PREFIX",
    "CliArgsSource",
    "CliFlag",
    "add_to_parser",
    "cli_dest",
    "cli_flag_name",
    "from_namespace",
]


FLAG_PREFIX: Final[str] = "--"
"""Префикс long-флага argparse."""


def cli_flag_name(key: ConfigKey) -> str:
    """ConfigKey → long-flag по единому алгоритму.

    Чистая функция, доступна публично — пригодится для генерации
    operator-доки и сообщений об ошибках.
    """
    return FLAG_PREFIX + "-".join(p.replace("_", "-") for p in key.parts)


def cli_dest(key: ConfigKey) -> str:
    """ConfigKey → dest-имя в argparse.Namespace.

    argparse автоматически выводит ``"--agent-run-model"`` →
    ``"agent_run_model"``; делаем то же явно, чтобы from_namespace
    смотрел на тот же атрибут.
    """
    return "_".join(p for p in key.parts).replace("-", "_")


@dataclass(frozen=True)
class CliFlag:
    """Декларативная связка ConfigKey и argparse-флага.

    Имя флага вычисляется из key через cli_flag_name() — никакого
    хардкода в декларации.

    action — argparse-action: ``"store"`` (default) или ``"append"`` для
    повторяемых флагов (``--agent-run-stop X --agent-run-stop Y``).

    help — строка для argparse ``--help``.
    """

    key: ConfigKey
    action: str = "store"
    help: str = ""

    @property
    def flag(self) -> str:
        return cli_flag_name(self.key)

    @property
    def dest(self) -> str:
        return cli_dest(self.key)


class CliArgsSource(ConfigSource):
    """ConfigSource поверх Mapping[ConfigKey, object].

    Принимает уже распакованный mapping; типичная сборка — через
    from_namespace(ns, FLAGS). Empty-string и None трактуются как
    «не задано» (пустые VS Code launch.json input-боксы) — следующий
    источник в цепочке отвечает.
    """

    def __init__(self, args: Mapping[ConfigKey, object | None]) -> None:
        self._args = {
            k: v for k, v in args.items() if v is not None and v != ""
        }

    def resolve(self, key: ConfigKey) -> object | None:
        return self._args.get(key)


def add_to_parser(
    parser: argparse.ArgumentParser,
    flags: Iterable[CliFlag],
) -> None:
    """Добавляет в parser argparse-аргументы по списку CliFlag.

    Имя флага и dest вычисляются из key. Все флаги получают
    ``default=None`` — отсутствие значения в Namespace ↔ «оператор не
    передал флаг» ↔ fall-through к следующим источникам в резолвере.
    """
    for f in flags:
        kwargs: dict[str, Any] = {"default": None, "dest": f.dest}
        if f.help:
            kwargs["help"] = f.help
        if f.action != "store":
            kwargs["action"] = f.action
        parser.add_argument(f.flag, **kwargs)


def from_namespace(
    ns: argparse.Namespace,
    flags: Iterable[CliFlag],
) -> CliArgsSource:
    """Собирает CliArgsSource из argparse.Namespace по тому же списку
    CliFlag, что был использован в add_to_parser.
    """
    return CliArgsSource({f.key: getattr(ns, f.dest, None) for f in flags})
