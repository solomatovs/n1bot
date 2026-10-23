"""Запечатанное соединение: профиль под открытым ключом получателя.

Клиент сервера инструментов (чат) хранит соединения пользователей и перед
вызовом отдаёт нужное серверу. Чтобы пароль и билет не читались по дороге и
не оседали в чужих логах, клиент шифрует профиль открытым ключом сервера:
открыть его может только сервер — владелец закрытого ключа. Формат —
компактный JWE (RFC 7516): согласование ключа ECDH-ES на P-256, содержимое
под A256GCM. Ключевая пара живёт у сервера (SealKeys), клиент получает
открытую половину (SealKey) и запечатывает ею (ConnectionSeal); о приёме
запечатанных соединений и о ключе сервер объявляет возможностью SealFeature
при подключении клиента. Параметр-соединение сервер помечает в схеме
ключом x-boba-connection с видом соединения, а что предложено вызывающему
(ConnectionOffer: запечатанное соединение пользователя, имена заданных
соединений сервера или то и другое), пишет стандартными ключами JSON Schema;
клиент читает предложение обратно той же SealedParamSchema. Модель на месте
параметра пишет имя соединения.

Ошибки:
RefusalError — значение не открывается: оно не запечатано, запечатано другим
    ключом, повреждено либо несёт содержимое не той формы; kind из
    ConnectionRefusal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from joserfc import jwe
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, GuestProtocol
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from boba.connections.marks import ConnectionRefusal
from boba.core.validation import ValidationText
from boba.identity.errors import RefusalError
from boba.toolkit.calls import SchemaMark

__all__ = [
    "ConnectionOffer",
    "ConnectionSeal",
    "OfferedSource",
    "SealFeature",
    "SealKey",
    "SealKeys",
    "SealedConnection",
    "SealedParamSchema",
]


class SealAlgorithm(StrEnum):
    """Параметры JWE запечатанного соединения и имена полей его заголовка."""

    KEY_AGREEMENT = "ECDH-ES"
    CONTENT = "A256GCM"
    CURVE = "P-256"
    HEADER_ALG = "alg"
    HEADER_ENC = "enc"
    HEADER_KID = "kid"


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


class ConnectionSeal:
    """Запечатывание соединений открытым ключом сервера на стороне клиента.

    Создаётся клиентом из ключа, который отдал сервер; каждое значение
    получает свой одноразовый ключ согласования, поэтому два запечатывания
    одного профиля не совпадают.
    """

    def __init__(self, key: SealKey) -> None:
        self._kid = key.kid
        self._public = ECKey.import_key(key.model_dump(exclude={"kid"}))

    def seal(self, connection: SealedConnection) -> str:
        protected = {
            SealAlgorithm.HEADER_ALG.value: SealAlgorithm.KEY_AGREEMENT.value,
            SealAlgorithm.HEADER_ENC.value: SealAlgorithm.CONTENT.value,
            SealAlgorithm.HEADER_KID.value: self._kid,
        }
        plaintext = connection.model_dump_json()

        return jwe.encrypt_compact(protected, plaintext, self._public)


class SealKeys:
    """Ключевая пара сервера инструментов для запечатанных соединений.

    Пара создаётся на старте сервера и живёт только в памяти процесса:
    открытую половину сервер отдаёт клиентам (public), закрытой открывает
    присланные значения (open). После перезапуска сервера пара другая, и
    значение, запечатанное прежним ключом, отвергается — клиент берёт новый
    ключ и запечатывает заново.
    """

    SEPARATORS: ClassVar[int] = 4
    """Компактный JWE — пять частей через точку."""

    def __init__(self) -> None:
        self._private = ECKey.generate_key(SealAlgorithm.CURVE.value)
        self._kid = self._private.thumbprint()

    def public(self) -> SealKey:
        jwk = self._private.as_dict(private=False)

        return SealKey.model_validate({"kid": self._kid, **jwk})

    def feature(self) -> SealFeature:
        """Возможность сервера с текущим ключом — для объявления клиентам."""
        return SealFeature(
            alg=SealAlgorithm.KEY_AGREEMENT.value,
            enc=SealAlgorithm.CONTENT.value,
            key=self.public(),
        )

    def sealed(self, value: str) -> bool:
        """Похоже ли значение на запечатанное: форма компактного JWE."""
        return value.count(".") == self.SEPARATORS

    def open(self, sealed: str) -> SealedConnection:
        if not self.sealed(sealed):
            msg = (
                f"opening a sealed connection failed: expected a compact JWE "
                f"sealed with key {self._kid!r}, got a string of "
                f"{len(sealed)} characters that is not one"
            )
            raise RefusalError(ConnectionRefusal.NOT_SEALED, msg)

        try:
            opened = jwe.decrypt_compact(sealed, self._key_of)
        except (JoseError, ValueError) as exc:
            msg = (
                f"opening a sealed connection with key {self._kid!r} failed: "
                f"the value is damaged or sealed for another server: {exc}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_DAMAGED, msg) from exc

        try:
            return SealedConnection.model_validate_json(opened.plaintext or b"")
        except ValidationError as exc:
            # from None: в input_value содержимого ездят секреты профиля
            msg = (
                f"opening a sealed connection with key {self._kid!r} failed: "
                f"the content is not a sealed connection: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_DAMAGED, msg) from None

    def _key_of(self, sealed: GuestProtocol) -> ECKey:
        """Закрытый ключ для значения; чужой kid — отказ до расшифровки."""
        kid = sealed.headers().get(SealAlgorithm.HEADER_KID.value)
        if kid != self._kid:
            msg = (
                f"opening a sealed connection failed: it is sealed with key "
                f"{kid!r}, the server key is {self._kid!r}; the client must "
                "read the current key of the server and seal the connection again"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        return self._private
