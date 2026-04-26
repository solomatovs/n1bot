"""CLI-arguments ConfigSource-реализация.

Алгоритм мапинга ConfigKey → CLI-flag (зеркальный env_name/toml_path):

    "--" + "-".join(p.replace("_", "-") for p in key.parts)

Например:

* ConfigKey("agent_run","model")              → --agent-run-model
* ConfigKey("agent_run","max_tokens")         → --agent-run-max-tokens
* ConfigKey("ext","chromadb","persist_path")  → --ext-chromadb-persist-path

Имя флага — deterministic функция от ключа, как BOBA_AGENT_RUN_MODEL
для env. Никаких алиасов и operator-side имён.

CliSource — автономный источник, симметричный EnvSource: ни списка
ключей, ни спецификации флагов. На каждый resolve(key) вычисляется
имя флага и сканируется sys.argv (поддерживаются формы
``--flag value`` и ``--flag=value``). Множественность для list-полей
делается через CSV в значении (``--agent-run-stop "X,Y"``) и
ParseCsvList валидатор — те же правила, что и для env.

Пустая строка («оператор стёр значение в launch.json input-боксе») и
None трактуются как «не задано» — следующий источник в цепочке отвечает.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Final

from boba.domain.core.config import ConfigKey, ConfigSource

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
    """Читает значение из argv по имени, выведенному из ConfigKey
    через cli_flag_name. По умолчанию sys.argv[1:]; для тестов можно
    передать явный argv.

    При нескольких вхождениях одного флага побеждает последнее (как у
    argparse store-action). Для list-полей оператор передаёт CSV в
    значении (``--agent-run-stop "X,Y"``) — раскладывает ParseCsvList.
    """

    def __init__(self, argv: Sequence[str] | None = None) -> None:
        self._argv: list[str] = list(argv) if argv is not None else list(sys.argv[1:])

    def resolve(self, key: ConfigKey) -> object | None:
        flag = cli_flag_name(key)
        prefix = flag + "="
        result: str | None = None
        i = 0
        while i < len(self._argv):
            tok = self._argv[i]
            if tok == flag:
                if i + 1 < len(self._argv):
                    result = self._argv[i + 1]
                i += 2
                continue
            if tok.startswith(prefix):
                result = tok[len(prefix) :]
            i += 1
        if result is None or result == "":
            return None
        return result
