"""CLI-arguments ConfigSource для Boba.

Раздаёт значения из argparse.Namespace по ConfigKey-биндингам.
Имя флага вычисляется из ключа симметрично env_name/toml_path:

    ConfigKey("agent_run","model") -> "--agent-run-model"
    (мирно с BOBA_AGENT_RUN_MODEL и [agent_run] model)

Обычно идёт первым в ChainedConfigResolver (highest priority).
"""

from boba.config.cli.source import (
    FLAG_PREFIX,
    CliArgsSource,
    CliFlag,
    add_to_parser,
    cli_dest,
    cli_flag_name,
    from_namespace,
)

__all__ = [
    "FLAG_PREFIX",
    "CliArgsSource",
    "CliFlag",
    "add_to_parser",
    "cli_dest",
    "cli_flag_name",
    "from_namespace",
]
