"""HeadingChunkerConfig — DTO с параметрами chunker'а.

Передаётся явно при создании в pipeline-плагине: pipeline-плагин читает
свою ConfigSection и собирает HeadingChunkerConfig — chunker не знает
про конфиг-фреймворк.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DEFAULT_CHUNK_OVERLAP", "DEFAULT_CHUNK_SIZE", "HeadingChunkerConfig"]


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150


@dataclass(frozen=True)
class HeadingChunkerConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
