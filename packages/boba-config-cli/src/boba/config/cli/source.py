"""argv-источник конфига. Имя флага — cli_flag_name(key)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
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


def cli_flag_name(key: ConfigKey) -> str:
    """ConfigKey → ``--a-b-c`` (mirror of env_name/toml_path)."""
    return FLAG_PREFIX + "-".join(p.replace("_", "-") for p in key.parts)


def cli_dest(key: ConfigKey) -> str:
    """ConfigKey → ``a_b_c`` (argparse default из cli_flag_name)."""
    return "_".join(key.parts).replace("-", "_")


class CliSource(ConfigSource):
    """Schema-driven argv-источник: bind_schema → argparse → resolve."""

    def __init__(self) -> None:
        self._values: dict[ConfigKey, object] = {}
        self._bound: bool = False

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        """Построить argparse из схемы, распарсить ``sys.argv``, заполнить ``_values``."""
        parser, dest_to_key = self._build_parser(items)
        namespace = parser.parse_args(sys.argv[1:])
        self._values = self._collect_values(namespace, dest_to_key)
        self._bound = True

    @staticmethod
    def _build_parser(
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> tuple[argparse.ArgumentParser, dict[str, ConfigKey]]:
        """argparse + dest→ConfigKey reverse-индекс; дубли ConfigKey'ев игнорируются."""
        parser = argparse.ArgumentParser(
            description="Boba CLI config (флаги = cli_flag_name(ConfigKey))."
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

    @staticmethod
    def _collect_values(
        namespace: argparse.Namespace,
        dest_to_key: dict[str, ConfigKey],
    ) -> dict[ConfigKey, object]:
        """argparse-namespace → ConfigKey-keyed словарь; None/'' пропускаем."""
        parsed = vars(namespace)
        return {
            key: parsed[dest]
            for dest, key in dest_to_key.items()
            if parsed[dest] not in (None, "")
        }

    def resolve(self, key: ConfigKey) -> object | None:
        """Lookup в ``_values``; падает RuntimeError, если bind_schema не вызван."""
        if not self._bound:
            raise RuntimeError(
                "CliSource.resolve() before bind_schema(); "
                "пройди через ConfigFactory.attach_sources(...) + build()."
            )
        return self._values.get(key)

    def describe(self, key: ConfigKey) -> str:
        """Operator-readable hint: ``CLI --a-b-c``."""
        return f"CLI {cli_flag_name(key)}"
