"""Kerberos-часть профилей соединений: способы аутентификации, режимы
делегирования, граница дампа. Пути кэшей и krb5.conf сюда не входят — их
держит рабочий каталог приложения (boba.krb.KerberosWorkspace).

Ошибки:
KerberosError — принципал неизвестен до вызова либо билет без SPN.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from boba.toolkit.types import SecretRevealing

__all__ = [
    "AcceptConfig",
    "CcacheKind",
    "ConstrainedDelegation",
    "DelegatedAuth",
    "Delegation",
    "DelegationMode",
    "ForwardedDelegation",
    "KerberosAuth",
    "KerberosAuthBase",
    "KerberosDump",
    "KerberosError",
    "KerberosMethod",
    "KerberosPasswordAuth",
    "KeytabAuth",
    "SignInTicket",
    "TicketAuth",
    "TicketSealError",
]


class KerberosError(Exception):
    """База ошибок kerberos: инфраструктурные варианты наследуют её."""


class TicketSealError(KerberosError):
    """Запечатанный билет входа не открывается: чужой ключ, порча или не тот формат."""


class CcacheKind(StrEnum):
    """Типы ccache, которые принимает приложение."""

    FILE = "FILE"


class KerberosMethod(StrEnum):
    """Способы kerberos-аутентификации; значение — поле method секции auth."""

    KEYTAB = "kerberos_keytab"
    PASSWORD = "kerberos_password"  # noqa: S105 — это имя метода, не секрет
    DELEGATED = "kerberos_delegated"
    TICKET = "kerberos_ticket"


class KerberosAuthBase(BaseModel):
    """Общее у kerberos-вариантов: имя сервиса и требуемый остаток билета."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(description="Способ; вариант сужает его до литерала.")
    service: str | None = Field(
        default=None,
        description=(
            "Имя kerberos-сервиса (krbsrvname): SPN собирается как "
            "<service>@<host>. None — имя по умолчанию у коннектора."
        ),
    )
    min_lifetime: int = Field(
        default=60,
        ge=0,
        description="Остаток билета (сек), ниже которого соединение не начинают.",
    )

    def source_principal(self) -> str:
        """Принципал, от чьего имени идёт соединение; у делегирования его нет."""
        msg = f"{type(self).__name__}: principal is known only at call time"
        raise KerberosError(msg)

    def trace(self) -> str:
        """Строка журнала: способ и под кем идём, без секретов."""
        try:
            principal = self.source_principal()
        except KerberosError:
            return f"auth={self.method}"

        if self.service is None:
            return f"auth={self.method} principal={principal}"

        return f"auth={self.method} principal={principal} service={self.service}"


class KeytabAuth(KerberosAuthBase):
    """Своя учётная запись строки: TGT выпускается ключом принципала."""

    method: Literal["kerberos_keytab"]

    principal: str = Field(
        min_length=1,
        description="Принципал, под которым получается TGT (user@REALM).",
    )
    keytab: str = Field(
        min_length=1,
        description="Путь к keytab с ключом принципала; наружу не уезжает.",
    )
    renew_lifetime: int = Field(
        default=86400,
        ge=0,
        description="renew_life запрашиваемого TGT (сек).",
    )

    def source_principal(self) -> str:
        return self.principal


class KerberosPasswordAuth(KerberosAuthBase):
    """Учётная запись строки по паролю: TGT выпускается kinit'ом."""

    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    method: Literal["kerberos_password"]

    principal: str = Field(
        min_length=1,
        description="Принципал, под которым получается TGT (user@REALM).",
    )
    password: SecretStr = Field(
        min_length=1,
        description="Пароль учётной записи; наружу не уезжает, в базе шифруется.",
    )

    def source_principal(self) -> str:
        return self.principal

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str:
        """Пароль маскируется всегда: в песочницу уезжает билет, а не он."""
        return str(value)


class DelegatedAuth(KerberosAuthBase):
    """В сервис идёт сам пользователь: креды даёт его вход в приложение."""

    method: Literal["kerberos_delegated"]


class TicketAuth(KerberosAuthBase):
    """Готовый билет одного вызова: креды тела инструмента в песочнице.

    Внутри ccache только билет к service — ни TGT, ни ключа принципала, так
    что получить билет к другому сервису телу нечем. Собирает эту секцию
    приложение, в конфиге и в таблице она запрещена. krb5.conf телу не
    передаётся: имена оно разбирает конфигом своей песочницы.
    """

    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    method: Literal["kerberos_ticket"]

    principal: str = Field(
        min_length=1,
        description="Принципал, чей билет лежит в ccache (user@REALM).",
    )
    service: str | None = Field(
        default=None,
        description="SPN назначения в виде service@host; у билета обязателен.",
    )
    ccache: SecretStr = Field(
        description="Содержимое FILE-ccache с одним сервисным билетом, base64.",
    )

    def source_principal(self) -> str:
        return self.principal

    def service_name(self) -> str:
        """SPN билета; None здесь невозможен — его ловит валидатор."""
        if self.service is None:
            msg = "ticket service is not set"
            raise KerberosError(msg)

        return self.service

    @field_validator("ccache")
    @classmethod
    def _check_ccache(cls, value: SecretStr) -> SecretStr:
        try:
            base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            msg = "ticket ccache: base64 expected"
            raise ValueError(msg) from exc

        return value

    @model_validator(mode="after")
    def _service_is_named(self) -> Self:
        if self.service is None:
            msg = "ticket service is required: it names the SPN of the ticket"
            raise ValueError(msg)

        name, sep, host = self.service.partition("@")
        if not sep or not name or not host:
            msg = f"ticket service {self.service!r} is not service@host"
            raise ValueError(msg)

        return self

    def ccache_bytes(self) -> bytes:
        return base64.b64decode(self.ccache.get_secret_value(), validate=True)

    @field_serializer("ccache", when_used="json")
    def _dump_ccache(self, value: SecretStr, info: SerializationInfo) -> str:
        """Байты билета уходят только в доверенный канал с REVEAL_SECRETS."""
        context = info.context
        if not isinstance(context, Mapping):
            return str(value)

        if not context.get(TicketAuth.REVEAL_SECRETS):
            return str(value)

        return value.get_secret_value()

    @classmethod
    def of_bytes(
        cls, principal: str, service: str, blob: bytes, min_lifetime: int
    ) -> TicketAuth:
        return cls(
            method=KerberosMethod.TICKET.value,
            principal=principal,
            service=service,
            ccache=SecretStr(base64.b64encode(blob).decode("ascii")),
            min_lifetime=min_lifetime,
        )


KerberosAuth: TypeAlias = Annotated[
    KeytabAuth | KerberosPasswordAuth | DelegatedAuth | TicketAuth,
    Field(discriminator="method"),
]
"""Kerberos-варианты авторизации соединения; различаются полем method."""


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

        if kind.upper() != CcacheKind.FILE:
            msg = f"service_ccache {value!r}: {CcacheKind.FILE} expected"
            raise ValueError(msg)

        return value


Delegation: TypeAlias = Annotated[
    ForwardedDelegation | ConstrainedDelegation,
    Field(discriminator="mode"),
]
"""Режим делегирования; выбирается явно полем mode."""


class SignInTicket(BaseModel):
    """Делегированные креды одного SSO-входа: содержимое FILE-ccache и срок.

    Constrained — evidence-тикет пользователя и TGT сервиса, forwarded — TGT
    пользователя. Живёт в JWT сессии запечатанным; процесс ничего не хранит.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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
