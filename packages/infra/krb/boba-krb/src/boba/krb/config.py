"""Конфигурация kerberos: клиентские креды из keytab, SPNEGO-accept, делегирование."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
)

__all__ = [
    "AcceptConfig",
    "CcacheConfig",
    "ClientKerberos",
    "DelegationConfig",
    "Kerberos",
    "KeytabConfig",
]


class CcacheConfig(BaseModel):
    """Клиентские креды по готовому тикету: keytab к ним не прилагается.

    Такими кредами работает тело инструмента в песочнице: TGT выпускает
    приложение своим keytab, а внутрь уезжает только ccache. Украденный
    тикет живёт до конца своего срока, украденный keytab — вечно.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["ccache"] = "ccache"
    """Различитель кредов: тикет уже выпущен, ключа принципала нет."""

    principal: str = Field(
        description="Принципал, чей тикет лежит в ccache (user@REALM).",
    )
    ccache: str = Field(
        description="Готовый ccache с типом, напр. FILE:/tmp/krb5cc_pg.",
    )
    krb5_config: str | None = Field(
        default=None,
        description="Свой krb5.conf; None — общий для процесса.",
    )
    min_lifetime: int = Field(
        default=60,
        ge=0,
        description=(
            "Остаток тикета (сек), ниже которого соединение не начинают: "
            "обновить его тело не может, keytab у него нет."
        ),
    )


class KeytabConfig(BaseModel):
    """Клиентские (initiate) креды соединения: свой keytab, принципал и свой ccache."""

    model_config = ConfigDict(extra="ignore")

    CCACHE_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"FILE", "DIR", "MEMORY", "KEYRING", "KCM"}
    )

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
            "Собственный ccache этих кредов с явным типом, "
            "напр. FILE:/run/boba/krb5cc_pg; общий с другими подсистемами не берём."
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

        if kind.upper() not in cls.CCACHE_TYPES:
            msg = f"ccache {value!r}: неизвестный тип {kind!r}"
            raise ValueError(msg)

        return value

    def sandboxed(self) -> CcacheConfig:
        """Те же креды для тела инструмента: тикет едет, keytab остаётся."""
        return CcacheConfig(
            principal=self.principal,
            ccache=self.ccache,
            krb5_config=self.krb5_config,
        )


class ClientKerberos:
    """Клиентские креды соединения: keytab у приложения, тикет у песочницы.

    Различаются по содержимому, а не по метке в конфиге: администратор
    описывает соединение keytab'ом, а вид без keytab собирает приложение
    само, когда отправляет конфиг внутрь.
    """

    @staticmethod
    def tag(value: object) -> str:
        if isinstance(value, KeytabConfig):
            return "keytab"

        if isinstance(value, CcacheConfig):
            return "ccache"

        if isinstance(value, Mapping) and "keytab" in value:
            return "keytab"

        return "ccache"


Kerberos: TypeAlias = Annotated[
    Annotated[KeytabConfig, Tag("keytab")] | Annotated[CcacheConfig, Tag("ccache")],
    Discriminator(ClientKerberos.tag),
]
"""Секция kerberos соединения: keytab-креды либо готовый тикет."""


class AcceptConfig(BaseModel):
    """Серверные (accept) креды SPNEGO: SPN сервиса и его keytab."""

    model_config = ConfigDict(extra="ignore")

    service_name: str = Field(
        description="SPN сервиса (HTTP/host@REALM).",
    )
    keytab: str = Field(
        description="Путь к keytab с ключом SPN; обычно /etc/krb5.keytab.",
    )


class DelegationConfig(BaseModel):
    """Куда класть делегированный ccache пользователя и продлевать ли тикет."""

    model_config = ConfigDict(extra="ignore")

    ccache_template: str = Field(
        default="MEMORY:agent-{principal}",
        description="Шаблон имени ccache на пользователя; подставляется {principal}.",
    )
    renew: bool = Field(
        default=True,
        description="Продлевать renewable-тикет по запросу при ошибке истечения.",
    )
