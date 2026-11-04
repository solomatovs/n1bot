"""Запечатанные значения OAuth без серверного хранения."""

from __future__ import annotations

import logging
from typing import ClassVar, TypeVar

from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)
ValueT = TypeVar("ValueT", bound=BaseModel)


class SealedValues:
    """Значения входа без хранения: модель шифруется ключом сервиса и уходит
    наружу строкой, обратно читается тем же ключом.

    Создаётся сборкой процесса из секрета [session] и соли печатей своей
    службы; им пользуются реестр
    клиентов и сервер авторизации. Сервису так нечего помнить между
    запросами: запись клиента едет в его идентификаторе, начатый вход — в
    адресе страницы входа, итог входа — в коде авторизации. Рестарт процесса
    и другой инстанс с тем же секретом читают их одинаково. Срок жизни
    значения проверяется при чтении по времени его выпуска.
    """

    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, secret: str, salt: str) -> None:
        self._box = Fernet(derive_jwt_key(high_entropy_material=secret, salt=salt))

    def seal(self, value: BaseModel) -> str:
        record = value.model_dump_json()

        return self._box.encrypt(record.encode(self.ENCODING)).decode(self.ENCODING)

    def opened(
        self, sealed: str, model: type[ValueT], ttl_sec: int | None
    ) -> ValueT | None:
        """Значение модели model; None — выпущено не этим сервисом, испорчено
        либо старше ttl_sec."""
        try:
            record = self._box.decrypt(sealed.encode(self.ENCODING), ttl=ttl_sec)
            return model.model_validate_json(record)
        except (InvalidToken, ValidationError):
            logger.info("sealed OAuth value is not accepted")
            return None
