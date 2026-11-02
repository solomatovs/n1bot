"""Модели запечатанных соединений и предложения соединений в JSON Schema.

Клиент и сервер используют общие DTO ключа, возможностей и профиля;
шифрование выполняет boba.connection_broker.crypto.

Ошибки: своих не выпускает; входные DTO проверяются Pydantic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from boba.toolkit.calls import SchemaMark

__all__ = [
    "ConnectionOffer",
    "OfferedSource",
    "SealFeature",
    "SealKey",
    "SealedConnection",
    "SealedParamSchema",
]


class OfferedSource(StrEnum):
    """Источник соединений, который предлагается вызывающему: SEALED —
    соединения пользователя, запечатанные клиентом; SERVER — заданные
    соединения сервера из его конфига."""

    SEALED = "sealed"
    SERVER = "server"


@dataclass(frozen=True)
class ConnectionOffer:
    """Что предложено вызывающему на месте одного параметра-соединения.

    kind — вид соединения параметра; sealed — принимается запечатанное
    соединение пользователя; names — видимые вызывающему заданные
    соединения сервера: имя → описание (у клиента, прочитавшего схему,
    описаний нет — пустые строки). Сервер пишет предложение в схему
    параметра (SealedParamSchema.declared), клиент читает его обратно
    (SealedParamSchema.offers).
    """

    kind: str
    sealed: bool
    names: Mapping[str, str]


class ConnectionParamText(StrEnum):
    """Описание параметра-соединения для модели клиента по тому, что
    предложено: SEALED — только свои соединения пользователя, SERVER — только
    заданные соединения сервера (listed — имена с описаниями), BOTH — то и
    другое. Пишет его SealedParamSchema.declared."""

    SEALED = (
        "Name of the {kind} connection to use. Pass the name of one of your "
        "own connections, the client seals it."
    )
    SERVER = "Name of the {kind} connection to use. Use one of: {listed}."
    BOTH = (
        "Name of the {kind} connection to use. Pass the name of one of your "
        "own connections, the client seals it, or one of the server "
        "connections: {listed}."
    )

    def render(self, kind: str, listed: str) -> str:
        return self.value.format(kind=kind, listed=listed)


class SealedParamSchema:
    """Схема параметра-соединения на проводе: сервер пишет, клиент читает.

    Параметр помечен ключом x-boba-connection (SchemaMark.CONNECTION) с видом
    соединения. Дальше — по предложению вызывающему: запечатанное значение
    объявлено стандартными ключами о содержимом строки — contentMediaType
    (компактная JWE, application/jose) и contentSchema (объект, чьё поле
    kind — константа вида); имена заданных соединений сервера — enum, их
    описания — в description; оба источника — anyOf из двух веток. Полную
    модель профиля сервер держит у себя и проверяет ею открытое значение; в
    схему она не идёт: её ссылки $ref генератор схемы инструмента не
    разрешает. Описание параметра пишется для модели чужого клиента.
    """

    MEDIA_TYPE: ClassVar[str] = "application/jose"
    KIND_FIELD: ClassVar[str] = "kind"

    def declared(self, offer: ConnectionOffer) -> dict[str, Any]:
        """Ключи схемы параметра по предложению offer: пометка с видом,
        ветка запечатанного значения и/или enum имён сервера, описание."""
        branches: list[dict[str, Any]] = []
        if offer.sealed:
            content = {
                "type": "object",
                SchemaMark.PROPERTIES: {
                    self.KIND_FIELD: {SchemaMark.CONST.value: offer.kind}
                },
                "required": [self.KIND_FIELD],
            }
            branches.append(
                {
                    "type": "string",
                    SchemaMark.MEDIA.value: self.MEDIA_TYPE,
                    SchemaMark.CONTENT.value: content,
                }
            )

        if offer.names:
            branches.append(
                {"type": "string", SchemaMark.ENUM.value: list(offer.names)}
            )

        described: list[str] = []
        for name, about in offer.names.items():
            described.append(f"{name} — {about}")

        listed = "; ".join(described)
        text = ConnectionParamText.BOTH
        if not offer.names:
            text = ConnectionParamText.SEALED

        if not offer.sealed:
            text = ConnectionParamText.SERVER

        declared: dict[str, Any] = {
            SchemaMark.CONNECTION.value: offer.kind,
            SchemaMark.DESCRIPTION.value: text.render(offer.kind, listed),
        }
        if len(branches) > 1:
            return {**declared, SchemaMark.ANY_OF.value: branches}

        return {**declared, **branches[0]}

    def offers(self, schema: Mapping[str, Any]) -> Mapping[str, ConnectionOffer]:
        """Параметры-соединения инструмента по его схеме schema: имя →
        предложение, прочитанное из схемы параметра."""
        offers: dict[str, ConnectionOffer] = {}
        properties = schema.get(SchemaMark.PROPERTIES)
        if not isinstance(properties, Mapping):
            return offers

        for param, kind in SchemaMark.CONNECTION.params(schema).items():
            if not isinstance(kind, str):
                continue

            declared = properties[param]
            branches: list[object] = [declared]
            nested = declared.get(SchemaMark.ANY_OF.value)
            if isinstance(nested, list):
                branches.extend(nested)

            sealed = False
            names: dict[str, str] = {}
            for branch in branches:
                if not isinstance(branch, Mapping):
                    continue

                if branch.get(SchemaMark.MEDIA.value) == self.MEDIA_TYPE:
                    sealed = True

                enum = branch.get(SchemaMark.ENUM.value)
                if not isinstance(enum, list):
                    continue

                for name in enum:
                    names[str(name)] = ""

            offers[param] = ConnectionOffer(kind=kind, sealed=sealed, names=names)

        return offers


class SealKey(BaseModel):
    """Открытый ключ сервера: им клиент запечатывает соединения.

    kid — идентификатор ключа; остальные поля — открытая половина в виде
    JWK. Модель плоская: её дамп — строка таблицы, которой сервер отдаёт
    ключ, а разбор строки — сам ключ.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kid: str = Field(min_length=1)
    kty: str
    crv: str
    x: str
    y: str


class SealFeature(BaseModel):
    """Возможность сервера «принимаю запечатанные соединения» и его ключ.

    Сервер объявляет её клиенту при подключении среди своих возможностей под
    идентификатором ID; клиент, который умеет запечатывать, берёт отсюда
    ключ и параметры JWE. Сервер без этой возможности соединений не
    принимает, и клиент их ему не отправляет.
    """

    ID: ClassVar[str] = "com.boba/connection-seal"

    model_config = ConfigDict(frozen=True, extra="ignore")

    alg: str
    enc: str
    key: SealKey


class SealedConnection(BaseModel):
    """Содержимое запечатанного значения: профиль, его имя и срок годности.

    profile — дамп профиля соединения с раскрытыми секретами, вид соединения
    лежит в нём полем kind. expires_at — срок годности. name — имя, которым
    соединение назвал вызывающий: сервер показывает его в итоге вызова на
    месте запечатанного значения.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    expires_at: AwareDatetime
    profile: Mapping[str, object]

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at
