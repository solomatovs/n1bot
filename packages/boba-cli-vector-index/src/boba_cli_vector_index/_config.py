"""Конфигурация CLI.

Источники (в порядке приоритета): CLI-флаг > env > env-file > TOML >
TOML-file > error.

Контракт общего ключа с ``boba-ext-chromadb`` — :class:`ConfigKey`
``("ext","chromadb","persist_path")``: оператор задаёт путь один раз
(env ``BOBA_EXT_CHROMADB_PERSIST_PATH`` или ``[ext.chromadb] persist_path``
в TOML), и тот же путь видит chainlit/agent-cli через
:class:`~boba_chromadb._config.ChromadbSection`. Импортно CLI на extension
не зависит — оператор может запустить индексирование на машине, где
extension не установлен.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from boba.domain.core.config import (
    ChainedConfigResolver,
    ConfigKey,
    FieldSpec,
)
from boba.domain.core.patterns import ConverterInputError
from boba.domain.core.validators import ChainConverter, ParseString, Required
from boba_config_env import EnvFileSource, EnvSource, env_name
from boba_config_toml import (
    CONFIG_PATH_ENV,
    TomlFileSource,
    TomlSource,
    load_toml,
)

_PERSIST_PATH: FieldSpec[str] = FieldSpec(
    key=ConfigKey("ext", "chromadb", "persist_path"),
    converter=ChainConverter(Required(), ParseString()),
)


class CliConfigError(Exception):
    """Ошибка конфига: например, не указан persist_path ни флагом, ни env."""


@dataclass(frozen=True)
class CliConfig:
    persist_path: str

    @classmethod
    def resolve(cls, *, persist_path_arg: str | None) -> CliConfig:
        """Собирает конфиг из CLI-аргумента и env/TOML. Бросает
        :class:`CliConfigError` если ни один источник не задал
        обязательное поле.
        """
        if persist_path_arg:
            return cls(persist_path=persist_path_arg)

        toml_data = load_toml(os.environ.get(CONFIG_PATH_ENV))
        resolver = ChainedConfigResolver(
            [
                EnvFileSource(),
                EnvSource(),
                TomlFileSource(toml_data),
                TomlSource(toml_data),
            ]
        )
        try:
            persist_path = _PERSIST_PATH.read(resolver)
        except ConverterInputError as e:
            env = env_name(_PERSIST_PATH.key)
            raise CliConfigError(
                f"persist_path is required: pass --persist-path, set env "
                f"{env}, или укажи [ext.chromadb] persist_path в TOML, на "
                f"который указывает {CONFIG_PATH_ENV}"
            ) from e
        return cls(persist_path=persist_path)
