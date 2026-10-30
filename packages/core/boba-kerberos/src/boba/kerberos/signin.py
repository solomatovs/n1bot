"""Kerberos-вход: серверные креды SPNEGO, режимы делегирования, билет входа
и порт его печати для JWT; разбор токена Negotiate из заголовка.

Ошибки:
TicketSealError — у реализации SignInCredentials при открытии билета.
"""

from __future__ import annotations

import base64
import time
from abc import abstractmethod
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AcceptConfig",
    "ConstrainedDelegation",
    "Delegation",
    "DelegationMode",
    "ForwardedDelegation",
    "NegotiateToken",
    "SignInCredentials",
    "SignInTicket",
]


class AcceptConfig(BaseModel):
    """Серверные (accept) креды SPNEGO: SPN сервиса и его keytab."""

    model_config = ConfigDict(extra="ignore")

    service_name: str = Field(
        description="SPN сервиса (HTTP/host@REALM).",
    )
    keytab: str = Field(
        description=(
            "Имя keytab с ключом SPN в каталоге krb5.conf приложения "
            "(conf/krb экземпляра)."
        ),
    )


class DelegationMode(StrEnum):
    """Как сервис получает креды пользователя для похода в бэкенд от его имени."""

    FORWARDED = "forwarded"
    """Неограниченное: браузер форвардит TGT пользователя, сервис им пользуется."""

    CONSTRAINED = "constrained"
    """Ограниченное (S4U2Proxy): сервис предъявляет KDC evidence-тикет пользователя
    и получает билет только к SPN из msDS-AllowedToDelegateTo."""


class ForwardedDelegation(BaseModel):
    """Неограниченное делегирование: форвардный TGT входа едет в билете входа.

    krb5.conf операций с делегированным тикетом — рабочий каталог kerberos
    процесса (KerberosWorkspace), в конфиге его нет.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["forwarded"] = "forwarded"


class ConstrainedDelegation(BaseModel):
    """Ограниченное делегирование: evidence-тикет входа плюс TGT сервиса.

    Сервису нужен собственный forwardable TGT (ключ учётки в keytab accept),
    KDC проверяет цель по msDS-AllowedToDelegateTo; у учётки не должно быть
    флага «sensitive» (NOT_DELEGATED). Форвардный TGT пользователя, если
    браузер всё же прислал, не принимается. Кэш TGT сервиса и krb5.conf
    берутся из рабочего каталога kerberos процесса (KerberosWorkspace).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["constrained"] = "constrained"


Delegation: TypeAlias = Annotated[
    ForwardedDelegation | ConstrainedDelegation,
    Field(discriminator="mode"),
]
"""Режим делегирования; выбирается явно полем mode."""


class SignInTicket(BaseModel):
    """Делегированные креды одного SSO-входа: содержимое FILE-ccache и срок.

    Constrained — evidence-тикет пользователя и TGT сервиса, forwarded — TGT
    пользователя. Живёт в JWT сессии запечатанным; процесс ничего не хранит.
    В json ccache едет base64-строкой.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    principal: str = Field(min_length=1)
    mode: DelegationMode
    ccache: bytes = Field(min_length=1)
    expires_at: int = Field(gt=0)
    """Конец делегированных кредов, unix-секунды."""

    def lifetime(self) -> int:
        """Остаток кредов, сек; 0 — истекли."""
        remaining = self.expires_at - int(time.time())
        if remaining < 0:
            return 0

        return remaining

    def needs_refresh(self, below_sec: int) -> bool:
        """Пора ли просить браузер обменяться заново: остаток меньше порога."""
        return self.lifetime() < below_sec


class SignInCredentials(Protocol):
    """Билет входа под печатью приложения: печать при SSO, открытие на вызове."""

    @abstractmethod
    def seal(self, ticket: SignInTicket) -> str: ...

    @abstractmethod
    def open(self, sealed: str) -> SignInTicket:
        """TicketSealError — чужой ключ, порча или не тот формат."""


class NegotiateToken:
    """Токен Negotiate из заголовка Authorization."""

    SCHEME: ClassVar[str] = "negotiate"

    @classmethod
    def of(cls, authorization: str) -> bytes | str:
        """Токен либо причина, почему его нет."""
        if not authorization:
            return "no Authorization header"

        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != cls.SCHEME:
            return f"unexpected auth scheme {scheme!r}"

        if not value:
            return f"unexpected auth scheme {scheme!r}"

        try:
            return base64.b64decode(value)
        except ValueError as e:
            return f"invalid base64 token: {e}"
