"""Токен и cookie входа: claims, порты выпуска и чтения, cookie целиком и чанками.

Claims одни у обоих приложений: identifier, display_name, metadata, exp, iat,
since — так их выпускает chainlit (без since) и так их читает studio. Правило
продления сессии — SessionRenewal: когда просить обмен и до какого потолка можно
перевыпускать токен без нового входа. Cookie длиннее лимита браузера едет чанками
name_0..name_n; сборка и разбор — только здесь.

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
from boba.identity.signin import SignedIn, SignInMetadata

__all__ = [
    "ClaimKey",
    "CookieJar",
    "CookieSpec",
    "RenewVerdict",
    "SessionClaims",
    "SessionRenewal",
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
    SINCE = "since"


class SessionClaims(BaseModel):
    """Тело токена входа: кто вошёл, metadata входа и сроки."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: str = Field(min_length=1)
    display_name: str = ""
    metadata: Mapping[str, object] = Field(default_factory=dict)
    exp: int = Field(gt=0)
    iat: int = Field(default=0, ge=0)
    """Момент выпуска; 0 — выпускающий его не пишет."""
    since: int = Field(default=0, ge=0)
    """Момент первого входа сессии; 0 — выпускающий его не пишет, тогда это iat."""

    @field_validator("display_name", mode="before")
    @classmethod
    def _none_as_empty(cls, value: object) -> object:
        # chainlit пишет display_name: null, когда имя не задано
        if value is None:
            return ""

        return value

    @classmethod
    def of_signed(cls, signed: SignedIn, issued_at: int, ttl_sec: int) -> SessionClaims:
        """Claims нового входа: сессия начинается сейчас."""
        return cls(
            identifier=signed.identifier,
            display_name=signed.display_name,
            metadata=signed.sign_in.render(),
            exp=issued_at + ttl_sec,
            iat=issued_at,
            since=issued_at,
        )

    def renewed(self, issued_at: int, ttl_sec: int) -> SessionClaims:
        """Те же вход и metadata с новым сроком; начало сессии сохраняется, а у токена
        без него началом становится этот перевыпуск.
        """
        return self.model_copy(
            update={
                "exp": issued_at + ttl_sec,
                "iat": issued_at,
                "since": self.started_at_or(issued_at),
            }
        )

    def started_at(self) -> int:
        """Начало сессии: since, а у токена без него — iat; 0 — неизвестно."""
        if self.since:
            return self.since

        return self.iat

    def started_at_or(self, default: int) -> int:
        started = self.started_at()
        if started:
            return started

        return default

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
            ClaimKey.SINCE.value: self.since,
        }

    def signed(self) -> SignedIn:
        return SignedIn(
            identifier=self.identifier,
            display_name=self.display_name,
            sign_in=self.sign_in(),
        )

    def sign_in(self) -> SignInMetadata:
        """Metadata входа из claims: форма JWT чужая, разбор — модель."""
        return SignInMetadata.parse(self.metadata)

    def ticket(self) -> DelegatedTicket | None:
        """Билет SSO-входа из metadata; None — вход был не через SPNEGO."""
        return self.sign_in().ticket()


class RenewVerdict(StrEnum):
    """Можно ли перевыпустить токен без нового входа."""

    RENEWABLE = "renewable"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


class SessionRenewal(BaseModel):
    """Правило продления сессии: срок токена, потолок сессии, порог сигнала и grace.

    Сигнал уходит, когда до конца токена меньше refresh_below_sec; перевыпуск
    принимает токен, чей exp прошёл не дольше grace_sec назад, и только пока сессия
    моложе max_sec от первого входа.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    REFRESH_BELOW_SEC: ClassVar[int] = 300
    GRACE_SEC: ClassVar[int] = 300

    ttl_sec: int = Field(gt=0)
    max_sec: int = Field(gt=0)
    refresh_below_sec: int = Field(gt=0)
    grace_sec: int = Field(ge=0)

    @classmethod
    def of(cls, ttl_sec: int, max_sec: int) -> SessionRenewal:
        return cls(
            ttl_sec=ttl_sec,
            max_sec=max_sec,
            refresh_below_sec=cls.REFRESH_BELOW_SEC,
            grace_sec=cls.GRACE_SEC,
        )

    def should_refresh(self, claims: SessionClaims, now: int) -> bool:
        return claims.exp - now < self.refresh_below_sec

    def verdict(self, claims: SessionClaims, now: int) -> RenewVerdict:
        if claims.exp + self.grace_sec < now:
            return RenewVerdict.EXPIRED

        # токен без iat и since: начало оцениваем по его сроку
        started = claims.started_at_or(claims.exp - self.ttl_sec)
        if now - started >= self.max_sec:
            return RenewVerdict.EXHAUSTED

        return RenewVerdict.RENEWABLE


class TokenIssuer(Protocol):
    """Выпуск токена входа по итогу входа и его перевыпуск по прежним claims."""

    @abstractmethod
    def issue(self, signed: SignedIn) -> str: ...

    @abstractmethod
    def renew(self, claims: SessionClaims) -> str: ...


class TokenReader(Protocol):
    """Чтение токена входа; негодный токен — TokenRejectedError."""

    @abstractmethod
    def read(self, token: str) -> SessionClaims: ...

    @abstractmethod
    def read_stale(self, token: str, grace_sec: int) -> SessionClaims:
        """Токен с верной подписью, чей exp прошёл не дольше grace_sec назад."""


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
