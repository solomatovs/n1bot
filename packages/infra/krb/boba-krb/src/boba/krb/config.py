"""Конфигурация kerberos: клиентские креды из keytab, SPNEGO-accept, делегирование."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    SecretStr,
    SerializationInfo,
    Tag,
    field_serializer,
    field_validator,
)

from boba.toolkit.types import SecretRevealing

__all__ = [
    "AcceptConfig",
    "ClientKerberos",
    "ConstrainedDelegation",
    "DelegatedConfig",
    "Delegation",
    "DelegationMode",
    "ForwardedDelegation",
    "Kerberos",
    "KerberosDump",
    "KerberosKind",
    "KeytabConfig",
    "TicketConfig",
]


class KerberosKind(StrEnum):
    """Виды клиентских кредов соединения; значение — поле kind секции."""

    KEYTAB = "keytab"
    DELEGATED = "delegated"
    TICKET = "ticket"


class KeytabConfig(BaseModel):
    """Клиентские (initiate) креды соединения: свой keytab, принципал и свой ccache."""

    model_config = ConfigDict(extra="ignore")

    CCACHE_TYPE: ClassVar[str] = "FILE"
    """TGT keytab-кредов живёт только в файле: процессные ccache чужие."""

    kind: Literal["keytab"] = "keytab"
    """Различитель кредов: тикет выпускается ключом принципала."""

    keytab: str = Field(
        description="Путь к keytab, содержащему ключ принципала.",
    )
    principal: str = Field(
        description="Принципал, под которым получается TGT (user@REALM).",
    )
    ccache: str = Field(
        description=(
            "Собственный FILE-ccache этих кредов, напр. FILE:/run/boba/krb5cc_pg; "
            "общий с другими подсистемами не берём."
        ),
    )
    krb5_config: str | None = Field(
        default=None,
        description="Свой krb5.conf; None — общий для процесса.",
    )
    min_lifetime: int = Field(
        default=300,
        ge=0,
        description="Остаток TGT (сек), ниже которого выпускается новый.",
    )
    renew_lifetime: int = Field(
        default=86400,
        ge=0,
        description="renew_life запрашиваемого TGT (сек).",
    )

    @field_validator("ccache")
    @classmethod
    def _check_ccache(cls, value: str) -> str:
        kind, sep, residual = value.partition(":")
        if not sep or not residual:
            msg = (
                f"ccache {value!r} без типа; ожидается "
                f"<тип>:<путь>, напр. FILE:/run/boba/krb5cc_pg"
            )
            raise ValueError(msg)

        if kind.upper() != cls.CCACHE_TYPE:
            msg = f"ccache {value!r}: keytab credentials need {cls.CCACHE_TYPE}"
            raise ValueError(msg)

        return value


class DelegatedConfig(BaseModel):
    """Креды делегирует пользователь сессии: билет выпускает приложение на вызов.

    Так описывается соединение в таблице: у строки нет ни keytab, ни ccache,
    она лишь требует, чтобы в бэкенд пошёл сам пользователь. Тело инструмента
    такую секцию не принимает — приложение заменяет её TicketConfig.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["delegated"] = "delegated"
    """Различитель кредов: тикет берётся у пользователя, а не у приложения."""

    min_lifetime: int = Field(
        default=60,
        ge=0,
        description="Остаток выпущенного билета (сек), ниже которого вызов не идёт.",
    )


class TicketConfig(BaseModel):
    """Ccache с одним сервисным билетом, байтами: креды тела на один вызов.

    Внутри нет TGT — только билет к service: тело не может получить билет
    ни к чему другому. Байты раскрываются в дампе только с REVEAL_SECRETS.
    """

    model_config = ConfigDict(extra="ignore")

    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    kind: Literal["ticket"] = "ticket"
    """Различитель кредов: готовый сервисный билет без TGT."""

    principal: str = Field(
        description="Принципал, чей билет лежит в ccache (user@REALM).",
    )
    service: str = Field(
        description="SPN назначения в виде service@host, для которого выпущен билет.",
    )
    ccache: SecretStr = Field(
        description="Содержимое FILE-ccache с одним сервисным билетом, base64.",
    )
    min_lifetime: int = Field(
        default=60,
        ge=0,
        description="Остаток билета (сек), ниже которого соединение не начинают.",
    )

    @field_validator("ccache")
    @classmethod
    def _check_ccache(cls, value: SecretStr) -> SecretStr:
        try:
            base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = "ticket ccache: base64 expected"
            raise ValueError(msg) from exc

        return value

    @field_serializer("ccache", when_used="json")
    def _dump_ccache(self, value: SecretStr, info: SerializationInfo) -> str:
        """Байты билета уходят только в доверенный канал с REVEAL_SECRETS."""
        context = info.context
        if not isinstance(context, Mapping):
            return str(value)

        if not context.get(TicketConfig.REVEAL_SECRETS):
            return str(value)

        return value.get_secret_value()

    def ccache_bytes(self) -> bytes:
        return base64.b64decode(self.ccache.get_secret_value(), validate=True)

    @classmethod
    def of_bytes(
        cls,
        principal: str,
        service: str,
        blob: bytes,
        min_lifetime: int,
    ) -> TicketConfig:
        return cls(
            principal=principal,
            service=service,
            ccache=SecretStr(base64.b64encode(blob).decode("ascii")),
            min_lifetime=min_lifetime,
        )


class ClientKerberos:
    """Клиентские креды соединения: keytab у приложения, билет у песочницы.

    Keytab-секция узнаётся по содержимому — в конфиге администратор kind не
    пишет; остальные виды различает поле kind.
    """

    @staticmethod
    def tag(value: object) -> str:
        if isinstance(value, BaseModel):
            return str(getattr(value, "kind", ""))

        if not isinstance(value, Mapping):
            return ""

        if "keytab" in value:
            return KerberosKind.KEYTAB.value

        kind: Any = value.get("kind")
        if isinstance(kind, str):
            return kind

        return ""


Kerberos: TypeAlias = Annotated[
    Annotated[KeytabConfig, Tag(KerberosKind.KEYTAB.value)]
    | Annotated[DelegatedConfig, Tag(KerberosKind.DELEGATED.value)]
    | Annotated[TicketConfig, Tag(KerberosKind.TICKET.value)],
    Discriminator(ClientKerberos.tag),
]
"""Секция kerberos соединения: keytab, делегирование пользователя или билет."""


class KerberosDump:
    """Дамп kerberos-секции профиля соединения на границе с песочницей.

    Дамп с раскрытыми секретами несёт только билет вызова: keytab наружу
    не уезжает, а billet-байты раскрываются лишь в этом контексте.
    """

    @staticmethod
    def json(
        value: KeytabConfig | DelegatedConfig | TicketConfig | None,
        context: object,
        what: str,
    ) -> dict[str, Any] | None:
        if value is None:
            return None

        if isinstance(value, TicketConfig):
            return value.model_dump(mode="json", context=context)

        if not isinstance(value, KeytabConfig):
            return value.model_dump(mode="json")

        if not isinstance(context, Mapping):
            return value.model_dump(mode="json")

        if not context.get(TicketConfig.REVEAL_SECRETS):
            return value.model_dump(mode="json")

        msg = (
            f"{what}: keytab credentials may not leave the application; "
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
    """Неограниченное делегирование: форвардный TGT входа живёт в своём ccache."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["forwarded"] = "forwarded"

    ccache_template: str = Field(
        description=(
            "Шаблон имени ccache на вход; подставляется {login} — случайная "
            "метка входа, одна на сессию."
        ),
    )
    renew: bool = Field(
        description="Продлевать renewable-TGT заранее, пока он ещё жив.",
    )
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

    ccache_template: str = Field(
        description=(
            "Шаблон имени ccache на вход; подставляется {login} — случайная "
            "метка входа, одна на сессию."
        ),
    )
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

        if kind.upper() != KeytabConfig.CCACHE_TYPE:
            msg = f"service_ccache {value!r}: {KeytabConfig.CCACHE_TYPE} expected"
            raise ValueError(msg)

        return value


Delegation: TypeAlias = Annotated[
    ForwardedDelegation | ConstrainedDelegation,
    Field(discriminator="mode"),
]
"""Режим делегирования; выбирается явно полем mode."""
