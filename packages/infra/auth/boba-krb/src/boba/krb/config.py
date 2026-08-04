"""Конфигурация kerberos: клиентские креды из keytab, SPNEGO-accept, делегирование."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["AcceptConfig", "DelegationConfig", "KeytabConfig"]


class KeytabConfig(BaseModel):
    """Клиентские (initiate) креды соединения: свой keytab, принципал и свой ccache."""

    model_config = ConfigDict(extra="ignore")

    CCACHE_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"FILE", "DIR", "MEMORY", "KEYRING", "KCM"}
    )

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
