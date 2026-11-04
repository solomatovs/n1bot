"""Браузерная cookie AID: параметры и передача длинного JWT частями."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

SameSite = Literal["lax", "strict", "none"]


class CookieSpec(BaseModel):
    """Cookie входа: имя, SameSite, срок и путь; Secure следует из SameSite=None."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    PATH: ClassVar[str] = "/"

    name: str = Field(min_length=1)
    samesite: SameSite
    ttl_sec: int = Field(gt=0)

    @property
    def path(self) -> str:
        return self.PATH

    @property
    def secure(self) -> bool:
        return self.samesite == "none"


class CookieJar:
    """Токен в cookie: целиком либо чанками name_0..name_n по CHUNK символов."""

    CHUNK: ClassVar[int] = 3000

    def __init__(self, name: str) -> None:
        if not name:
            msg = "cookie jar: expected a non-empty cookie name, got ''"
            raise ValueError(msg)

        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def pieces(self, token: str) -> list[tuple[str, str]]:
        """Пары ключ-значение, которыми токен ставится в ответ."""
        if len(token) <= self.CHUNK:
            return [(self._name, token)]

        return list(self._chunks(token))

    def _chunks(self, token: str) -> Iterator[tuple[str, str]]:
        for index, start in enumerate(range(0, len(token), self.CHUNK)):
            yield f"{self._name}_{index}", token[start : start + self.CHUNK]

    def token_of(self, present: Mapping[str, str]) -> str | None:
        """Токен из cookie запроса; None — cookie входа нет."""
        whole = present.get(self._name)
        if whole:
            return whole

        parts: list[str] = []
        index = 0
        while True:
            chunk = present.get(f"{self._name}_{index}")
            if chunk is None:
                break

            parts.append(chunk)
            index += 1

        joined = "".join(parts)
        if not joined:
            return None

        return joined

    def ours(self, present: Mapping[str, str]) -> set[str]:
        """Ключи cookie входа среди присланных: целиком и все чанки."""
        found: set[str] = set()
        for key in present:
            if key == self._name:
                found.add(key)
                continue
            if key.startswith(f"{self._name}_"):
                found.add(key)

        return found

    def stale(
        self, present: Mapping[str, str], fresh: list[tuple[str, str]]
    ) -> set[str]:
        """Ключи прежнего токена, которые новый не перезаписывает."""
        keys = self.ours(present)
        for key, _ in fresh:
            keys.discard(key)

        return keys
