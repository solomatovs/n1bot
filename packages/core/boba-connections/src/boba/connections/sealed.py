"""Запечатанное соединение: профиль под открытым ключом получателя.

Клиент сервера инструментов (чат) хранит соединения пользователей и перед
вызовом отдаёт нужное серверу. Чтобы пароль и билет не читались по дороге и
не оседали в чужих логах, клиент шифрует профиль открытым ключом сервера:
открыть его может только сервер — владелец закрытого ключа. Формат —
компактный JWE (RFC 7516): согласование ключа ECDH-ES на P-256, содержимое
под A256GCM. Ключевая пара живёт у сервера (SealKeys), клиент получает
открытую половину (SealKey) и запечатывает ею (ConnectionSeal).

Ошибки:
RefusalError — значение не открывается: оно не запечатано, запечатано другим
    ключом, повреждено либо несёт содержимое не той формы; kind из
    ConnectionRefusal.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from joserfc import jwe
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, GuestProtocol
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from boba.connections.marks import ConnectionRefusal
from boba.identity.errors import RefusalError
from boba.toolkit.failure import ValidationText

__all__ = ["ConnectionSeal", "SealKey", "SealKeys", "SealedConnection"]


class SealAlgorithm(StrEnum):
    """Параметры JWE запечатанного соединения и имена полей его заголовка."""

    KEY_AGREEMENT = "ECDH-ES"
    CONTENT = "A256GCM"
    CURVE = "P-256"
    HEADER_ALG = "alg"
    HEADER_ENC = "enc"
    HEADER_KID = "kid"


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


class SealedConnection(BaseModel):
    """Содержимое запечатанного значения: профиль и для кого он запечатан.

    profile — дамп профиля соединения с раскрытыми секретами, вид соединения
    лежит в нём полем kind. login — пользователь, которому клиент выдал
    соединение: сервер сверяет его с вошедшим. expires_at — срок годности.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    def open(self, sealed: str) -> SealedConnection:
        if sealed.count(".") != self.SEPARATORS:
            msg = (
                f"opening a sealed connection failed: expected a compact JWE "
                f"sealed with key {self._kid!r}, got {sealed[:40]!r}"
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
                f"{kid!r}, the server key is {self._kid!r}; take the current "
                "key of the server and seal the connection again"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        return self._private
