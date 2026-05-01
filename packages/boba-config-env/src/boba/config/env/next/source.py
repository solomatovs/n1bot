"""env-источник под confignext: BOBA_<SEG>__<SEG>__<INDEX> → $seg.seg[index]."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from boba_next import ConfigSource, ConfigValue, StringValue
from boba_next.path import (
    ConfigPath,
    ConfigPathParseError,
    IndexSegment,
    NameSegment,
    Segment,
)

__all__ = [
    "ENV_FILE_SUFFIX",
    "ENV_PREFIX",
    "ENV_SEPARATOR",
    "EnvFileSource",
    "EnvSource",
]

logger = logging.getLogger(__name__)


ENV_PREFIX: Final[str] = "BOBA"
ENV_SEPARATOR: Final[str] = "__"
ENV_FILE_SUFFIX: Final[str] = "_FILE"


class EnvSource(ConfigSource):
    """Плоский snapshot из process env.

    Convention: `BOBA_<SEG>__<SEG>__...`, разделитель сегментов — `__`.
    Сегмент из одних цифр → IndexSegment(int(seg)).
    Иначе → NameSegment(seg.lower()).
    Все значения отдаются как StringValue (env-варсы — всегда строки).
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        priority: int = 300,
        name: str | None = None,
    ) -> None:
        self._env = dict(env) if env is not None else dict(os.environ)
        self._priority = priority
        self._name = name or "env"

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        result: dict[ConfigPath, ConfigValue] = {}
        for key, value in self._env.items():
            if not key.startswith(ENV_PREFIX + "_"):
                continue
            if key.endswith(ENV_FILE_SUFFIX):
                continue
            path = _decode_env_key(key)
            if path is None:
                continue
            result[path] = StringValue(value)
        return result

    def describe(self, path: ConfigPath) -> str:
        return f"env {_encode_env_key(path)}=<value>"


class EnvFileSource(ConfigSource):
    """env-варианты вида `BOBA_<...>_FILE=/path/to/secret`: значение — содержимое файла."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        priority: int = 400,
        name: str | None = None,
    ) -> None:
        self._env = dict(env) if env is not None else dict(os.environ)
        self._priority = priority
        self._name = name or "env_file"

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        result: dict[ConfigPath, ConfigValue] = {}
        for key, raw_path in self._env.items():
            if not key.startswith(ENV_PREFIX + "_"):
                continue
            if not key.endswith(ENV_FILE_SUFFIX):
                continue
            base = key[: -len(ENV_FILE_SUFFIX)]
            path = _decode_env_key(base)
            if path is None:
                continue
            secret_path = Path(raw_path)
            if not secret_path.is_file():
                continue
            content = secret_path.read_text(encoding="utf-8").rstrip("\n")
            result[path] = StringValue(content)
        return result

    def describe(self, path: ConfigPath) -> str:
        return f"env {_encode_env_key(path)}{ENV_FILE_SUFFIX}=<path-to-secret-file>"


def _decode_env_key(env_key: str) -> ConfigPath | None:
    """`BOBA_AGENT__MAX_ITERATIONS` → ConfigPath('$agent.max_iterations')."""
    body = env_key[len(ENV_PREFIX) + 1 :]
    if not body:
        return None
    parts = body.split(ENV_SEPARATOR)
    segments: list[Segment] = []
    for part in parts:
        if not part:
            return None
        if part.isdigit():
            segments.append(IndexSegment(int(part)))
            continue
        try:
            segments.append(NameSegment(part.lower()))
        except ConfigPathParseError:
            return None
    return ConfigPath(tuple(segments))


def _encode_env_key(path: ConfigPath) -> str:
    parts: list[str] = [ENV_PREFIX]
    for seg in path:
        key = seg.mapping_key()
        if key is not None:
            parts.append(key.upper())
            continue
        index = seg.list_index()
        if index is not None:
            parts.append(str(index))
            continue
        raise ValueError(f"cannot encode segment for env: {seg!r}")
    head = parts[0]
    rest = ENV_SEPARATOR.join(parts[1:])
    return f"{head}_{rest}" if rest else head
