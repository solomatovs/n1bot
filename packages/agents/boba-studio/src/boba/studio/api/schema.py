"""OpenAPI-документ API без хоста: приложение собирается с заглушками входов.

Ошибки:
RuntimeError — заглушки входов вызваны: схема их не зовёт, вызов — ошибка сборки.
"""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar

from boba.auth import AuthService, AuthUsers, JwtTokens
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.connections.manifest import ConnectionTypes
from boba.identity.api import (
    AuthenticatedUser,
    Authenticator,
)
from boba.identity.locks import MemoryLiveLocks
from boba.identity.signin import SignedIn
from boba.identity.token import CookieSpec, SessionRenewal
from boba.messaging import MemoryMessageBus
from boba.messaging.bus import ListenerState, StaticBusWatch
from boba.runtime.refs import RuntimeRefs
from boba.runtime.users import UsersTable
from boba.studio.api.app import ApiAccess, ApiApp
from boba.studio.api.signin import PageUrls, SignInWiring
from boba.toolrun.registry import ToolRegistry
from boba.workflow_engine.service import WorkflowService

__all__ = ["OpenApiDocument"]


class NoOne(Authenticator):
    """Вход для схемы: никого не пускает."""

    async def user_of_token(self, token: str) -> AuthenticatedUser | None:
        return None


class NoUsers(AuthUsers):
    """Строки users для схемы: никого не читает и не пишет."""

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        msg = "users table is not part of the schema stand"
        raise RuntimeError(msg)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        msg = "users table is not part of the schema stand"
        raise RuntimeError(msg)


class OpenApiDocument:
    """Схема API как JSON; входы приложения — заглушки, их никто не зовёт."""

    PROFILE: ClassVar[str] = "schema"
    COOKIE: ClassVar[str] = "access_token"
    JWT_KEY: ClassVar[str] = "schema-only"

    @classmethod
    def render(cls) -> dict[str, Any]:
        access = ApiAccess(NoOne(), cls.COOKIE, cls._no_users)
        app = ApiApp.build(cls._refs(), access, cls._profiles(), cls._signin())

        return app.openapi()

    @classmethod
    def dump(cls) -> str:
        return json.dumps(cls.render(), ensure_ascii=False, indent=2)

    @classmethod
    def main(cls) -> None:
        sys.stdout.write(cls.dump())
        sys.stdout.write("\n")

    @classmethod
    def _signin(cls) -> SignInWiring:
        """Маршруты входа в схеме есть; провайдера паролей у схемы нет."""
        auth = AuthService(
            tokens=JwtTokens(cls.JWT_KEY, 1),
            cookie=CookieSpec(name=cls.COOKIE, samesite="lax", ttl_sec=1),
            password=None,
            sso=None,
            users=NoUsers(),
            renewal=SessionRenewal.of(1, 1 * 24),
        )

        return SignInWiring(
            auth=auth,
            sso_url="",
            page=PageUrls(root="/workflow", login="/workflow/login", home="/workflow"),
        )

    @classmethod
    def _no_users(cls) -> UsersTable:
        msg = "users table is not part of the schema stand"
        raise RuntimeError(msg)

    @classmethod
    def _profiles(cls) -> ChatProfiles:
        profile = ChatProfileConfig.model_construct(
            display_name=cls.PROFILE, default=True
        )

        return ChatProfiles({cls.PROFILE: profile})

    @classmethod
    def _refs(cls) -> RuntimeRefs:
        return RuntimeRefs(
            tool_registry=cls._no_registry,
            workflow_service=cls._no_service,
            connection_store=cls._no_store,
            connection_types=ConnectionTypes.discover,
            credentials=cls._no_credentials,
            live_locks=lambda: MemoryLiveLocks("stand", 20),
            heartbeat_sec=1.0,
            bus_watch=lambda: StaticBusWatch(ListenerState.LISTENING),
            message_bus=lambda: MemoryMessageBus("stand"),
        )

    @staticmethod
    async def _no_registry() -> ToolRegistry:
        raise RuntimeError(OpenApiDocument._stub_called("tool registry"))

    @staticmethod
    async def _no_service() -> WorkflowService:
        raise RuntimeError(OpenApiDocument._stub_called("workflow service"))

    @staticmethod
    def _no_store() -> Any:
        raise RuntimeError(OpenApiDocument._stub_called("connection store"))

    @staticmethod
    def _no_credentials() -> KerberosCredentialSource:
        return KerberosCredentialSource(None, NoRefresh())

    @staticmethod
    def _stub_called(name: str) -> str:
        return f"{name} is not available while rendering the OpenAPI schema"


if __name__ == "__main__":
    OpenApiDocument.main()
