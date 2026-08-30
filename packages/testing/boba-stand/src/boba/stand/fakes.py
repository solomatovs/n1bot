"""Заглушки тестов: случайные секреты и адреса, за которыми нет сети."""

import secrets
from enum import StrEnum


class FakeSecret(StrEnum):
    """Заглушки секретов для тестов: значение рождается на запуске, не в коде."""

    LDAP_BIND = secrets.token_hex(8)
    AUTH = secrets.token_hex(8)
    DB = secrets.token_hex(8)
    DB_OTHER = secrets.token_hex(8)
    HTTP_BASIC = secrets.token_hex(8)
    HTTP_BEARER = secrets.token_hex(8)


class FakeUrl(StrEnum):
    """Адреса-заглушки: запросы уходят в ASGI-приложение, а не в сеть."""

    BASE = "https://boba"
    WORKSPACE = "https://boba/workspace"
    LOOPBACK_SCHEME = "http"
    LOOPBACK_HOST = "127.0.0.1"

    @classmethod
    def loopback(cls, port: int, path: str = "") -> str:
        """Адрес локального стенда: сервер поднимается тестом, TLS ему негде взять."""
        return f"{cls.LOOPBACK_SCHEME}://{cls.LOOPBACK_HOST}:{port}{path}"
