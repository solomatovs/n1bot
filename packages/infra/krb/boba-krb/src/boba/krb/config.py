"""Конфигурация kerberos: SPNEGO-accept, режимы делегирования, граница дампа.

Ошибки: своих не выпускает; дамп keytab/пароля наружу — ValueError.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.krb.auth import (
    DelegatedAuth,
    KerberosAuth,
    KerberosWorkspace,
    TicketAuth,
)

__all__ = [
    "AcceptConfig",
    "ConstrainedDelegation",
    "Delegation",
    "DelegationMode",
    "ForwardedDelegation",
    "KerberosDump",
]


class KerberosDump:
    """Дамп kerberos-секции соединения на границе с песочницей.

    Дамп с раскрытыми секретами несёт только билет вызова: ни keytab, ни
    пароль наружу не уезжают, а байты билета раскрываются лишь в этом
    контексте.
    """

    @staticmethod
    def json(
        value: KerberosAuth | None, context: object, what: str
    ) -> dict[str, Any] | None:
        if value is None:
            return None

        if isinstance(value, TicketAuth):
            return value.model_dump(mode="json", context=context)

        if isinstance(value, DelegatedAuth):
            return value.model_dump(mode="json")

        if not isinstance(context, Mapping):
            return value.model_dump(mode="json")

        if not context.get(TicketAuth.REVEAL_SECRETS):
            return value.model_dump(mode="json")

        msg = (
            f"{what}: {value.method} credentials may not leave the application; "
            "issue a call ticket before revealing the config"
        )
        raise ValueError(msg)


class AcceptConfig(BaseModel):
    """Серверные (accept) креды SPNEGO: SPN сервиса и его keytab."""

    model_config = ConfigDict(extra="ignore")

    service_name: str = Field(
        description="SPN сервиса (HTTP/host@REALM).",
    )
    keytab: str = Field(
        description="Путь к keytab с ключом SPN; обычно /etc/krb5.keytab.",
    )


class DelegationMode(StrEnum):
    """Как сервис получает креды пользователя для похода в бэкенд от его имени."""

    FORWARDED = "forwarded"
    """Неограниченное: браузер форвардит TGT пользователя, сервис им пользуется."""

    CONSTRAINED = "constrained"
    """Ограниченное (S4U2Proxy): сервис предъявляет KDC evidence-тикет пользователя
    и получает билет только к SPN из msDS-AllowedToDelegateTo."""


class ForwardedDelegation(BaseModel):
    """Неограниченное делегирование: форвардный TGT входа едет в билете входа."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["forwarded"] = "forwarded"

    krb5_config: str = Field(
        description=(
            "krb5.conf для операций с делегированным тикетом и для билетов, "
            "выпущенных из него."
        ),
    )


class ConstrainedDelegation(BaseModel):
    """Ограниченное делегирование: evidence-тикет входа плюс TGT сервиса.

    Сервису нужен собственный forwardable TGT (ключ учётки в keytab accept),
    KDC проверяет цель по msDS-AllowedToDelegateTo; у учётки не должно быть
    флага «sensitive» (NOT_DELEGATED). Форвардный TGT пользователя, если
    браузер всё же прислал, не принимается.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["constrained"] = "constrained"

    service_ccache: str = Field(
        description=(
            "FILE-ccache для собственного TGT сервиса, которым делается S4U2Proxy, "
            "напр. FILE:/run/boba/krb5cc_http."
        ),
    )
    krb5_config: str = Field(
        description=(
            "krb5.conf для операций с evidence-тикетом и для билетов, "
            "выпущенных из него."
        ),
    )

    @field_validator("service_ccache")
    @classmethod
    def _check_service_ccache(cls, value: str) -> str:
        kind, sep, residual = value.partition(":")
        if not sep or not residual:
            msg = f"service_ccache {value!r}: expected FILE:<path>"
            raise ValueError(msg)

        if kind.upper() != KerberosWorkspace.CCACHE_TYPE:
            msg = f"service_ccache {value!r}: {KerberosWorkspace.CCACHE_TYPE} expected"
            raise ValueError(msg)

        return value


Delegation: TypeAlias = Annotated[
    ForwardedDelegation | ConstrainedDelegation,
    Field(discriminator="mode"),
]
"""Режим делегирования; выбирается явно полем mode."""
