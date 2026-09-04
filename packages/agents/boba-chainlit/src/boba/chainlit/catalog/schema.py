"""OpenAPI-документ JSON API каталога без приложения: маршруты собираются на
пустом FastAPI со стендовым профилем; сервис никто не зовёт.

Ошибки:
RuntimeError — заглушка сервиса вызвана: схема её не зовёт, вызов — ошибка сборки.
"""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar

from fastapi import APIRouter, FastAPI

from boba.catalog_service import CatalogService
from boba.chainlit.catalog.api import CatalogApi, CatalogUrl
from boba.chainlit.catalog.subjects import ChainlitSubjects
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.connection_broker.api import ConnectionsApi
from boba.connection_broker.service import UserConnectionsService
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import CredentialSource
from boba.connections.manifest import ConnectionTypes
from boba.messaging import MessageBus

__all__ = ["CatalogOpenApi"]


class CatalogOpenApi:
    """Схема JSON API каталога как JSON."""

    TITLE: ClassVar[str] = "boba catalog api"
    PROFILE: ClassVar[str] = "schema"

    @classmethod
    def render(cls) -> dict[str, Any]:
        app = FastAPI(title=cls.TITLE)
        router = APIRouter(prefix=CatalogUrl.PREFIX.value)
        subjects = ChainlitSubjects(cls._profiles())
        CatalogApi(cls._no_service, subjects).mount(router)
        ConnectionsApi(
            UserConnectionsService(cls._no_store),
            subjects.of_request,
            cls._no_credentials,
            cls._no_bus,
            ConnectionTypes.discover(),
        ).mount(router)
        app.include_router(router)

        return app.openapi()

    @classmethod
    def dump(cls) -> str:
        return json.dumps(cls.render(), ensure_ascii=False, indent=2)

    @classmethod
    def main(cls) -> None:
        sys.stdout.write(cls.dump())
        sys.stdout.write("\n")

    @classmethod
    def _profiles(cls) -> ChatProfiles:
        profile = ChatProfileConfig.model_construct(
            display_name=cls.PROFILE, default=True
        )

        return ChatProfiles({cls.PROFILE: profile})

    @staticmethod
    async def _no_service() -> CatalogService:
        msg = "catalog service is not available while rendering the OpenAPI schema"
        raise RuntimeError(msg)

    @staticmethod
    def _no_store() -> ConnectionStore:
        msg = "connection store is not available while rendering the OpenAPI schema"
        raise RuntimeError(msg)

    @staticmethod
    def _no_credentials() -> CredentialSource:
        msg = "credentials are not available while rendering the OpenAPI schema"
        raise RuntimeError(msg)

    @staticmethod
    def _no_bus() -> MessageBus:
        msg = "message bus is not available while rendering the OpenAPI schema"
        raise RuntimeError(msg)


if __name__ == "__main__":
    CatalogOpenApi.main()
