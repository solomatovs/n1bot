"""Запечатанное соединение: профиль под открытым ключом получателя.

Клиент сервера инструментов (чат) хранит соединения пользователей и перед
вызовом отдаёт нужное серверу. Чтобы пароль и билет не читались по дороге и
не оседали в чужих логах, клиент шифрует профиль открытым ключом сервера:
открыть его может только сервер — владелец закрытого ключа. Формат —
компактный JWE (RFC 7516): согласование ключа ECDH-ES на P-256, содержимое
под A256GCM. Ключевая пара живёт у сервера (SealKeys), клиент получает
открытую половину (SealKey) и запечатывает ею (ConnectionSeal); о приёме
запечатанных соединений и о ключе сервер объявляет возможностью SealFeature
при подключении клиента. Какой параметр
инструмента — соединение и какого вида, сервер пишет в схему параметра
стандартными ключами JSON Schema, а клиент по ним же читает
(SealedParamSchema); модели клиент показывает на этом месте имя соединения.

Ошибки:
RefusalError — значение не открывается: оно не запечатано, запечатано другим
    ключом, повреждено либо несёт содержимое не той формы; kind из
    ConnectionRefusal.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
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

from boba.connections.base import ConnectionBase
from boba.connections.marks import ConnectionRefusal
from boba.identity.errors import RefusalError
from boba.toolkit.failure import ValidationText

__all__ = [
    "ConnectionSeal",
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


class SealedParamSchema:
    """Схема параметра-соединения на проводе: сервер пишет, клиент читает.

    Параметр — строка с запечатанным профилем, и схема говорит это
    стандартными ключами JSON Schema о содержимом строки: contentMediaType
    — тип компактной JWE (application/jose), contentSchema — JSON-схема
    модели профиля, которую сервер ждёт внутри; вид соединения — константа
    её поля kind. Своих ключей в схеме нет: чужой клиент читает её обычным
    разбором JSON Schema. Определения вложенных моделей лежат под самим
    параметром, и их ссылки $ref считаются от корня схемы инструмента.
    """

    MEDIA_TYPE: ClassVar[str] = "application/jose"
    MEDIA_KEY: ClassVar[str] = "contentMediaType"
    CONTENT_KEY: ClassVar[str] = "contentSchema"
    PROPERTIES: ClassVar[str] = "properties"
    DEFINITIONS: ClassVar[str] = "$defs"
    KIND_FIELD: ClassVar[str] = "kind"
    CONST_KEY: ClassVar[str] = "const"

    def declared(self, param: str, model: type[ConnectionBase]) -> dict[str, Any]:
        """Ключи схемы параметра param: тип содержимого и схема профиля model."""
        under_param = "/".join((self.PROPERTIES, param, self.CONTENT_KEY))
        template = f"#/{under_param}/{self.DEFINITIONS}/{{model}}"

        return {
            self.MEDIA_KEY: self.MEDIA_TYPE,
            self.CONTENT_KEY: model.model_json_schema(ref_template=template),
        }

    def kind_of(self, declared: Mapping[str, Any]) -> str | None:
        """Вид соединения параметра по его схеме; None — параметр не соединение."""
        if declared.get(self.MEDIA_KEY) != self.MEDIA_TYPE:
            return None

        content = declared.get(self.CONTENT_KEY)
        if not isinstance(content, Mapping):
            return None

        properties = content.get(self.PROPERTIES)
        if not isinstance(properties, Mapping):
            return None

        kind = properties.get(self.KIND_FIELD)
        if not isinstance(kind, Mapping):
            return None

        const = kind.get(self.CONST_KEY)
        if not isinstance(const, str):
            return None

        return const

    def kinds_of(self, schema: Mapping[str, Any]) -> dict[str, str]:
        """Параметры-соединения инструмента по его схеме: имя → вид."""
        return dict(self._kinds_of(schema))

    def _kinds_of(self, schema: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
        properties = schema.get(self.PROPERTIES)
        if not isinstance(properties, Mapping):
            return

        for param, declared in properties.items():
            if not isinstance(declared, Mapping):
                continue

            kind = self.kind_of(declared)
            if kind is None:
                continue

            yield param, kind


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
    """Содержимое запечатанного значения: профиль и для кого он запечатан.

    profile — дамп профиля соединения с раскрытыми секретами, вид соединения
    лежит в нём полем kind. login — пользователь, которому клиент выдал
    соединение: сервер сверяет его с вошедшим. expires_at — срок годности.
    name — имя, которым соединение назвал вызывающий: сервер показывает его
    в итоге вызова на месте запечатанного значения.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    login: str = Field(min_length=1)
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
