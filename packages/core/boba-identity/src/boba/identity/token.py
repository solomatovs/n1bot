"""Токен и cookie входа: claims, порты выпуска и чтения, cookie целиком и чанками.

Claims одни у обоих приложений: identifier, display_name, metadata, exp, iat —
так их выпускает chainlit и так их читает studio. Cookie длиннее лимита браузера
едет чанками name_0..name_n; сборка и разбор — только здесь.

Ошибки:
TokenRejectedError — токен не принят: истёк, подпись чужая либо тело не разбирается.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from boba.identity.context import DelegatedTicket
from boba.identity.signin import SignedIn

__all__ = [
    "ClaimKey",
    "CookieJar",
    "CookieSpec",
    "SessionClaims",
    "TokenAlgorithm",
    "TokenIssuer",
    "TokenReader",
    "TokenRejectedError",
    "TokenRejection",
]

SameSite = Literal["lax", "strict", "none"]


class TokenAlgorithm(StrEnum):
    """Алгоритм подписи JWT входа."""

    HS256 = "HS256"


class TokenRejection(StrEnum):
    """Почему токен не принят."""

    EXPIRED = "expired"
    SIGNATURE = "signature"
    MALFORMED = "malformed"


class TokenRejectedError(Exception):
    """Токен входа не принят; причина различима для журнала и ответа."""

    def __init__(self, reason: TokenRejection, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ClaimKey(StrEnum):
    """Поля JWT входа."""

    IDENTIFIER = "identifier"
    DISPLAY_NAME = "display_name"
    METADATA = "metadata"
    EXP = "exp"
    IAT = "iat"


class SessionClaims(BaseModel):
    """Тело токена входа: кто вошёл, metadata входа и сроки."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: str = Field(min_length=1)
    display_name: str = ""
    metadata: Mapping[str, object] = Field(default_factory=dict)
    exp: int = Field(gt=0)
    iat: int = Field(default=0, ge=0)
    """Момент выпуска; 0 — выпускающий его не пишет."""

    @field_validator("display_name", mode="before")
    @classmethod
    def _none_as_empty(cls, value: object) -> object:
        # chainlit пишет display_name: null, когда имя не задано
        if value is None:
            return ""

        return value

    @classmethod
    def of_signed(cls, signed: SignedIn, issued_at: int, ttl_sec: int) -> SessionClaims:
        return cls(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=dict(signed.metadata),
            exp=issued_at + ttl_sec,
            iat=issued_at,
        )

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> SessionClaims:
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise TokenRejectedError(TokenRejection.MALFORMED, str(exc)) from exc

    def render(self) -> dict[str, Any]:
        return {
            ClaimKey.IDENTIFIER.value: self.identifier,
            ClaimKey.DISPLAY_NAME.value: self.display_name,
            ClaimKey.METADATA.value: dict(self.metadata),
            ClaimKey.EXP.value: self.exp,
            ClaimKey.IAT.value: self.iat,
        }

    def signed(self) -> SignedIn:
        return SignedIn(
            identifier=self.identifier,
            display_name=self.display_name,
            metadata=self.metadata,
        )

    def ticket(self) -> DelegatedTicket | None:
        """Билет SSO-входа из metadata; None — вход был не через SPNEGO."""
        return DelegatedTicket.of_metadata(self.metadata)


class TokenIssuer(Protocol):
    """Выпуск токена входа по итогу входа."""

    @abstractmethod
    def issue(self, signed: SignedIn) -> str: ...


class TokenReader(Protocol):
    """Чтение токена входа; негодный токен — TokenRejectedError."""

    @abstractmethod
    def read(self, token: str) -> SessionClaims: ...


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
            raise ValueError("cookie name is empty")

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
