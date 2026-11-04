"""Сохранение входа и обновление прав: общий сценарий для AID и OAuth."""

from __future__ import annotations

from boba.identity.api import StoredUser, UserRows
from boba.identity.authentication import AuthenticatedPrincipal, AuthenticatedSession
from boba.identity.errors import AuthenticationError
from boba.identity.signin import ProfileCatalog, SessionAccess, SignInResult


class SessionService:
    """Подтверждённая сессия из результата входа и актуальных данных users.

    Сервис не выпускает токены и не знает о cookie, OAuth или HTTP.
    """

    def __init__(self, users: UserRows, profiles: ProfileCatalog) -> None:
        self._users = users
        self._access = SessionAccess(profiles)

    async def accept(
        self,
        signed: SignInResult,
        *,
        previous: AuthenticatedPrincipal | None = None,
    ) -> AuthenticatedSession:
        if previous is not None:
            if previous.identity != signed.identity:
                raise AuthenticationError(
                    "session renewal resolved to another identity"
                )
            await self.stored(previous)
        stored = await self._users.upsert(signed.identity, signed.managed_roles)
        if previous is not None and previous.id != stored.id:
            raise AuthenticationError(
                "the stored session user has changed; sign in again"
            )
        access = self._access.current(signed.access, stored.roles)
        if previous is not None:
            access = access.model_copy(
                update={"profiles": access.profiles & previous.profiles}
            )
        return AuthenticatedSession(
            principal=AuthenticatedPrincipal(
                id=stored.id, identity=stored.identity, access=access
            ),
            credential=signed.credential,
            renewal=signed.renewal,
        )

    async def stored(self, principal: AuthenticatedPrincipal) -> StoredUser:
        stored = await self._users.stored_by_id(principal.id)
        if stored is None or stored.identity != principal.identity:
            raise AuthenticationError(
                "the session user no longer matches a stored identity"
            )
        return stored

    async def current(
        self, principal: AuthenticatedPrincipal
    ) -> AuthenticatedPrincipal:
        """Актуальные роли; ранее выданный набор профилей может только сужаться."""
        stored = await self.stored(principal)
        access = self._access.current(principal.access, stored.roles)
        return principal.model_copy(update={"access": access})
