"""Extension-функции для AgentBuilder, специфичные для web-chainlit."""

from __future__ import annotations

import os

from boba.agent.builder import AgentBuilder
from boba.config.source.toml import TomlFileSource, TomlSource


def use_toml_config(
    builder: AgentBuilder,
    path: str | os.PathLike[str] | None = None,
) -> AgentBuilder:
    """Подключить TOML-источники к общему `ConfigBundle` билдера."""
    return (
        builder
        .use_config_source(TomlFileSource(path))
        .use_config_source(TomlSource(path))
    )
