"""CLI-arguments :class:`ConfigSource` для Boba.

Public API::

    from boba.config.cli import CliArgsSource, CliFlagBinding, from_namespace

Подключение в bootstrap'е CLI-приложения::

    import argparse
    from boba.domain.core.config import ChainedConfigResolver, ConfigKey
    from boba.config.cli import CliArgsSource

    parser = argparse.ArgumentParser()
    parser.add_argument("--persist-path", dest="persist_path")
    args = parser.parse_args()

    cli_source = CliArgsSource({
        ConfigKey("ext", "chromadb", "persist_path"): args.persist_path,
    })
    resolver = ChainedConfigResolver([cli_source, EnvSource(), TomlSource(...)])
"""

from boba.config.cli.source import (
    CliArgsSource,
    CliFlagBinding,
    from_namespace,
)

__all__ = [
    "CliArgsSource",
    "CliFlagBinding",
    "from_namespace",
]
