"""argv-источник под confignext: --ext.chromadb.tools.kb_search.enabled=true."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, Sequence

from boba.config.path import ConfigSource
from boba.value import ConfigValue, StringValue
from boba.config.path import (
    ConfigPath,
    ConfigPathParseError,
    IndexSegment,
    NameSegment,
    Segment,
)

__all__ = ["CliSource", "parse_argv_path"]

logger = logging.getLogger(__name__)


class CliSource(ConfigSource):
    """argv-источник.

    Convention:
      --<dotted-path>=<value>           # `--ext.chromadb.enabled=true`
      --<dotted-path> <value>           # `--ext.chromadb.enabled true`
      --<dotted-path>                   # bare flag → "true"
      Индексы: --models[0]=... либо --models.0=...
      Тире в сегментах конвертируются в подчёркивания (--max-iterations == max_iterations).

    Все значения — StringValue.
    """

    def __init__(
        self,
        argv: Sequence[str] | None = None,
        *,
        priority: int = 500,
        name: str | None = None,
    ) -> None:
        self._argv = list(argv) if argv is not None else list(sys.argv[1:])
        self._priority = priority
        self._name = name or "cli"

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        result: dict[ConfigPath, ConfigValue] = {}
        i = 0
        while i < len(self._argv):
            token = self._argv[i]
            if not token.startswith("--"):
                i += 1
                continue
            body = token[2:]
            if not body:
                i += 1
                continue
            if "=" in body:
                key_str, value = body.split("=", 1)
                path = parse_argv_path(key_str)
                if path is not None:
                    result[path] = StringValue(value)
                i += 1
                continue
            path = parse_argv_path(body)
            if path is None:
                i += 1
                continue
            next_token = self._argv[i + 1] if i + 1 < len(self._argv) else None
            if next_token is None or next_token.startswith("--"):
                result[path] = StringValue("true")
                i += 1
            else:
                result[path] = StringValue(next_token)
                i += 2
        return result

    def describe(self, path: ConfigPath) -> str:
        return f"cli --{_render_argv_path(path)}=<value>"


def parse_argv_path(text: str) -> ConfigPath | None:
    """`ext.chromadb.tools.kb_search.enabled` или `models[0]` → ConfigPath."""
    if not text:
        return None
    normalized = text.replace("-", "_")
    segments: list[Segment] = []
    i = 0
    while i < len(normalized):
        ch = normalized[i]
        if ch == ".":
            i += 1
            continue
        if ch == "[":
            end = normalized.find("]", i + 1)
            if end == -1:
                return None
            digits = normalized[i + 1 : end]
            if not digits.isdigit():
                return None
            segments.append(IndexSegment(int(digits)))
            i = end + 1
            continue
        # Накапливаем имя до следующего "." или "["
        j = i
        while j < len(normalized) and normalized[j] not in ".[":
            j += 1
        token = normalized[i:j]
        if not token:
            return None
        if token.isdigit():
            segments.append(IndexSegment(int(token)))
        else:
            try:
                segments.append(NameSegment(token))
            except ConfigPathParseError:
                return None
        i = j
    if not segments:
        return None
    return ConfigPath(tuple(segments))


def _render_argv_path(path: ConfigPath) -> str:
    parts: list[str] = []
    for i, seg in enumerate(path):
        key = seg.mapping_key()
        if key is not None:
            parts.append(key if i == 0 else f".{key}")
            continue
        index = seg.list_index()
        if index is not None:
            parts.append(f"[{index}]")
            continue
        raise ValueError(f"cannot render segment for cli: {seg!r}")
    return "".join(parts)
