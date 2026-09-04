"""Субъект JSON API каталога по входу chainlit: пользователь из cookie или
Authorization, роли из metadata входа, профиль каталога по умолчанию, секреты
входа для инструментов. Один разбор для маршрутов каталога и для общего API
соединений, смонтированного под тем же префиксом.

Ошибки:
HTTPException 401 — входа нет, пользователь не сохранён слоем данных или его
    id не uuid строки users.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import HTTPException, Request

from boba.chainlit.infra.session import ChainlitSession
from boba.chat.profiles import ChatProfiles
from boba.identity.api import ApiSubject
from boba.identity.context import Subject
from boba.identity.signin import SignInMetadata
from chainlit.auth import get_current_user, reuseable_oauth
from chainlit.user import PersistedUser, User

__all__ = ["ChainlitSubjects", "SignedIn", "UserOfRequest"]

UserOfRequest = Callable[[Request], Awaitable[User | PersistedUser | None]]
"""Пользователь входа по запросу; стенды подменяют его заглушкой."""


class SignedIn:
    """Пользователь входа chainlit по запросу: токен из cookie или Authorization.

    Обёртка вместо прямого Depends(get_current_user): схема безопасности
    chainlit ломает генерацию OpenAPI, а тесты подменяют одну зависимость.
    """

    @staticmethod
    async def user(request: Request) -> User | PersistedUser | None:
        token = await reuseable_oauth(request)
        if token is None:
            return None

        return await get_current_user(token)


class ChainlitSubjects:
    """Субъект каталога по пользователю входа: роли входа, профиль каталога
    по умолчанию, билет входа как секреты."""

    def __init__(
        self, profiles: ChatProfiles, users: UserOfRequest = SignedIn.user
    ) -> None:
        self._profiles = profiles
        self._users = users

    def of_user(self, current_user: User | PersistedUser | None) -> ApiSubject:
        """Ошибки:
        HTTPException 401 — пользователь не сохранён слоем данных.
        """
        if not isinstance(current_user, PersistedUser):
            got = type(current_user).__name__
            msg = f"catalog api expects a signed-in persisted user, got {got}"
            raise HTTPException(status_code=401, detail=msg)

        try:
            user_id = UUID(current_user.id)
        except ValueError as exc:
            msg = f"user id {current_user.id!r} is not the users.id uuid: {exc}"
            raise HTTPException(status_code=401, detail=msg) from exc

        roles = ChainlitSession.roles_of(current_user)
        profile = self._profiles.default_name()
        subject = Subject.of_user(user_id, current_user.identifier, roles, profile)
        sign_in = SignInMetadata.parse(current_user.metadata)

        return ApiSubject(subject=subject, credential=sign_in.credential())

    async def of_request(self, request: Request) -> ApiSubject:
        """Резолвер для общего API соединений: субъект по запросу."""
        current_user = await self._users(request)

        return self.of_user(current_user)
