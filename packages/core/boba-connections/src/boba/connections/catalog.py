"""Открытые записи и имена операций каталога соединений, без хранилища и транспорта."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ConnectionTool(StrEnum):
    LIST = "connection_list"
    SEARCH = "connection_search"


class CatalogEntry(BaseModel):
    """Описание соединения для пользователя: адрес без credentials и имя для вызова."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    kind: str
    url: str
    host: str = ""
    description: str = ""
