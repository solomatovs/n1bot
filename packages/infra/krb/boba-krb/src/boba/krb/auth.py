"""Способы kerberos-аутентификации соединения: чем добывается билет к сервису.

Вариант описывается полем method и несёт ровно те поля, которые нужны ему
самому. Пути кэшей и krb5.conf сюда не входят: их держит приложение
(KerberosWorkspace), поэтому две строки не могут случайно поделить один
ccache, а администратор не выбирает их за приложение.

Ошибки:
KerberosError — рабочий каталог kerberos не настроен либо принципал непригоден
    как имя файла кэша.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self, TypeAlias

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

from boba.krb.errors import KerberosError
from boba.toolkit.types import SecretRevealing

__all__ = [
    "DelegatedAuth",
    "KerberosAuth",
    "KerberosAuthBase",
    "KerberosMethod",
    "KerberosPasswordAuth",
    "KerberosWorkspace",
    "KerberosWorkspaceConfig",
    "KeytabAuth",
    "TicketAuth",
]


class KerberosMethod(StrEnum):
    """Способы kerberos-аутентификации; значение — поле method секции auth."""

    KEYTAB = "kerberos_keytab"
    PASSWORD = "kerberos_password"  # noqa: S105 — это имя метода, не секрет
    DELEGATED = "kerberos_delegated"
    TICKET = "kerberos_ticket"


class KerberosWorkspace:
    """Каталог кэшей и krb5.conf приложения; настраивается один раз на старте.

    Кэш выделяется на пару «принципал — источник кредов»: у двух строк с
    разными keytab один файл не появится даже при одинаковом принципале.
    """

    CCACHE_TYPE: ClassVar[str] = "FILE"
    PREFIX: ClassVar[str] = "krb5cc"
    TAG_LENGTH: ClassVar[int] = 12
    DIR_MODE: ClassVar[int] = 0o700

    _settings: ClassVar[dict[str, str]] = {}

    @classmethod
    def configure(cls, krb5_config: str, ccache_dir: str) -> None:
        """Ставит рабочий каталог процесса; каталог создаётся приватным."""
        os.makedirs(ccache_dir, mode=cls.DIR_MODE, exist_ok=True)
        os.chmod(ccache_dir, cls.DIR_MODE)
        cls._settings = {"krb5_config": krb5_config, "ccache_dir": ccache_dir}

    @classmethod
    def krb5_config(cls) -> str:
        return cls._setting("krb5_config")

    @classmethod
    def ccache_of(cls, principal: str, source: str) -> str:
        """Имя ccache этих кредов: принципал в имени, источник в хвосте."""
        digest = hashlib.sha256(f"{principal}|{source}".encode()).hexdigest()
        tag = digest[: cls.TAG_LENGTH]
        safe = re.sub(r"[^\w.@-]", "_", principal)
        path = os.path.join(cls._setting("ccache_dir"), f"{cls.PREFIX}_{safe}_{tag}")

        return f"{cls.CCACHE_TYPE}:{path}"

    @classmethod
    def _setting(cls, name: str) -> str:
        value = cls._settings.get(name)
        if value is None:
            msg = (
                "kerberos workspace is not configured: call "
                "KerberosWorkspace.configure(krb5_config, ccache_dir) on startup"
            )
            raise KerberosError(msg)

        return value


class KerberosWorkspaceConfig(BaseModel):
    """Секция [krb]: где приложение держит krb5.conf и кэши билетов."""

    model_config = ConfigDict(extra="ignore")

    config: str = Field(
        min_length=1,
        description="krb5.conf приложения; тот же путь виден телу в песочнице.",
    )
    ccache_dir: str = Field(
        min_length=1,
        description=(
            "Каталог кэшей билетов приложения; создаётся приватным, имена "
            "выделяются по принципалу и источнику кредов."
        ),
    )

    def apply(self) -> None:
        """Ставит рабочий каталог процесса; зовётся один раз на старте."""
        KerberosWorkspace.configure(self.config, self.ccache_dir)


class KerberosAuthBase(BaseModel):
    """Общее у kerberos-вариантов: имя сервиса и требуемый остаток билета."""

    model_config = ConfigDict(extra="forbid")

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

    def ccache(self) -> str:
        """Свой кэш этих кредов: имя выделяет приложение по keytab."""
        return KerberosWorkspace.ccache_of(self.principal, self.keytab)


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

    def ccache(self) -> str:
        """Свой кэш этих кредов: пароль в имя не попадает."""
        return KerberosWorkspace.ccache_of(self.principal, self.method)

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
