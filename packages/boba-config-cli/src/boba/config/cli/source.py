"""CLI-arguments ConfigSource-реализация.

Источник пассивный: на ``bind_schema`` строит ``argparse.ArgumentParser``
из набора ``(ConfigKey, FieldSpec)``-пар, единократно парсит argv и
сохраняет результат в локальном словаре. ``resolve`` делает простой
lookup — никакого ручного скана argv.

Алгоритм мапинга ConfigKey → CLI-flag (зеркальный env_name/toml_path):

    "--" + "-".join(p.replace("_", "-") for p in key.parts)

Например:

* ConfigKey("agent_run","model")              → --agent-run-model
* ConfigKey("agent_run","max_tokens")         → --agent-run-max-tokens
* ConfigKey("ext","chromadb","persist_path")  → --ext-chromadb-persist-path

Имя флага — deterministic функция от ключа, как BOBA_AGENT_RUN_MODEL
для env. Никаких алиасов и operator-side имён.

``--help`` генерируется argparse'ом из ``FieldSpec.description`` —
оператор видит полный список флагов с описаниями. Опечатка в имени
флага роняет процесс с argparse-ошибкой (exit-code 2), а не уходит в
тихий ``Required()`` где-то глубже.

Список и multi-value поля передаются как один CSV-аргумент
(``--agent-run-stop "X,Y"``); за раскладку отвечает ParseCsvList в
схеме поля. Booleans пока тоже идут как ``--flag value`` — переход
на ``store_true`` возможен, когда converter-цепочки начнут сообщать
тип через SchemaContributor.

Пустая строка («оператор стёр значение в launch.json input-боксе») и
None трактуются как «не задано» — следующий источник в цепочке отвечает.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from typing import Any, Final

from boba.domain.core.config import ConfigKey, ConfigSource
from boba.domain.core.declaration import FieldSpec

__all__ = [
    "FLAG_PREFIX",
    "CliSource",
    "cli_dest",
    "cli_flag_name",
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
    ``"agent_run_model"``; делаем то же явно — пригодится, если
    оператор хочет дублировать флаг в своём user-facing argparse.
    """
    return "_".join(key.parts).replace("-", "_")


class CliSource(ConfigSource):
    """Schema-driven argv-источник.

    Конструктор не делает ничего, кроме сохранения hint'а на argv.
    Реальная работа — в ``bind_schema``: строит argparse-парсер из
    ``(ConfigKey, FieldSpec)``-пар (которые ConfigFactory даёт после
    регистрации всех секций, включая extension'ы), парсит argv и
    кладёт значения в локальный dict, индексированный по ConfigKey.

    ``resolve`` после этого — O(1) lookup без I/O.

    Если ``resolve`` вызван до ``bind_schema``, источник падает с
    понятной ошибкой — это знак, что инициализация прошла мимо
    ConfigFactory.build (которая дёргает bind_schema автоматически).
    """

    def __init__(self, argv: Sequence[str] | None = None) -> None:
        self._argv_hint: list[str] | None = (
            list(argv) if argv is not None else None
        )
        self._values: dict[ConfigKey, object] = {}
        self._bound: bool = False

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        parser, dest_to_key = self._build_parser(items)
        namespace = parser.parse_args(self._effective_argv())
        self._values = self._collect_values(namespace, dest_to_key)
        self._bound = True

    @staticmethod
    def _build_parser(
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> tuple[argparse.ArgumentParser, dict[str, ConfigKey]]:
        """Построить argparse-парсер и параллельный dest → ConfigKey
        реверс-индекс. Дубликаты ConfigKey между секциями (extension и
        локальная мини-секция могут заявлять один и тот же ключ) даём
        первому вхождению — argparse отвергает повторный add_argument,
        а значение всё равно одно."""
        parser = argparse.ArgumentParser(
            description=(
                "Boba CLI configuration. Все значения — поля "
                "зарегистрированных ConfigSection'ов; имена флагов "
                "вычисляются из ConfigKey через cli_flag_name."
            ),
        )
        dest_to_key: dict[str, ConfigKey] = {}
        for key, fld in items:
            dest = cli_dest(key)
            if dest in dest_to_key:
                continue
            parser.add_argument(
                cli_flag_name(key),
                dest=dest,
                default=None,
                help=fld.description or None,
            )
            dest_to_key[dest] = key
        return parser, dest_to_key

    def _effective_argv(self) -> list[str]:
        """Конструкторный argv-hint, иначе sys.argv без [0] (имя
        исполняемого файла)."""
        if self._argv_hint is not None:
            return self._argv_hint
        return sys.argv[1:]

    @staticmethod
    def _collect_values(
        namespace: argparse.Namespace,
        dest_to_key: dict[str, ConfigKey],
    ) -> dict[ConfigKey, object]:
        """Перевести argparse-namespace в ConfigKey-keyed словарь.

        Пустая строка (оператор стёр значение в launch.json input-боксе)
        и None трактуются как «не задано» — следующий источник
        в цепочке возьмёт на себя.
        """
        parsed = vars(namespace)  # argparse гарантирует наличие всех dest
        values: dict[ConfigKey, object] = {}
        for dest, key in dest_to_key.items():
            value = parsed[dest]
            if value is None or value == "":
                continue
            values[key] = value
        return values

    def resolve(self, key: ConfigKey) -> object | None:
        if not self._bound:
            raise RuntimeError(
                "CliSource.resolve() called before bind_schema(); "
                "инициализируй через ConfigFactory(...).attach_sources([...]) "
                "+ build() — фабрика сама даст источнику схему."
            )
        return self._values.get(key)

    def describe(self, key: ConfigKey) -> str:
        return f"CLI {cli_flag_name(key)}"
