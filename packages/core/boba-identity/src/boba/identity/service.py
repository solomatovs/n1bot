"""Публичный адрес службы, используемый клиентами и издателем токенов."""

from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


class PublicAddress(BaseModel):
    """Публичный адрес и префикс приложения."""

    public_url: str = Field(min_length=1)
    url_prefix: str = Field(default="", pattern=r"^(/[^/]+)*$")


class ServiceAddress(PublicAddress):
    """Адрес службы с префиксом, под которым она принимает запросы."""

    @model_validator(mode="after")
    def _prefix_of_public_url(self) -> Self:
        path = urlsplit(self.public_url).path.rstrip("/")
        if path != self.url_prefix:
            msg = (
                f"public_url {self.public_url!r} has the path {path!r}, expected "
                f"the url_prefix {self.url_prefix!r}: the service answers under "
                "url_prefix and builds its OAuth addresses from public_url"
            )
            raise ValueError(msg)
        return self
