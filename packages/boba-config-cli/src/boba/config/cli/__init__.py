"""CLI-arguments ConfigSource для Boba.

CliSource() сам ходит за sys.argv. Имя флага вычисляется из ключа
симметрично env_name/toml_path:

    ConfigKey("agent_run","model") -> "--agent-run-model"
    (мирно с BOBA_AGENT_RUN_MODEL и [agent_run] model)

Обычно идёт первым в ChainedConfigResolver (highest priority).
"""

from boba.config.cli.source import (
    FLAG_PREFIX,
    CliSource,
    cli_dest,
    cli_flag_name,
)

__all__ = [
    "FLAG_PREFIX",
    "CliSource",
    "cli_dest",
    "cli_flag_name",
]
