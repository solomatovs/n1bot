"""SessionService persists provider results and reconciles current access."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from boba.auth.service import SessionService
from boba.identity.api import StoredUser, UserRows
from boba.identity.authentication import AccessGrant, RenewalKind
from boba.identity.context import CredentialAbsence, NoUserCredential
from boba.identity.errors import AuthenticationError
from boba.identity.principal import UserIdentity
from boba.identity.signin import ProfileCatalog, SignInResult

pytestmark = pytest.mark.anyio

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
IDENTITY = UserIdentity(realm="corporate", login="alice")


class Profiles(ProfileCatalog):
    def granted_by_roles(self, user_roles: frozenset[str]) -> frozenset[str]:
        if "admin" in user_roles:
            return frozenset({"general", "admin"})
        if "read" in user_roles:
            return frozenset({"general"})
        return frozenset()

    def known(self, name: str) -> bool:
        return name in {"general", "admin"}


class Users(UserRows):
    def __init__(self) -> None:
        self.rows: dict[UserIdentity, StoredUser] = {}
        self.managed: dict[UserIdentity, frozenset[str]] = {}

    async def stored(self, identity: UserIdentity) -> StoredUser | None:
        return self.rows.get(identity)

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        return next((row for row in self.rows.values() if row.id == user_id), None)

    async def upsert(
        self, identity: UserIdentity, roles: frozenset[str] | None = None
    ) -> StoredUser:
        old = self.rows.get(identity)
        if roles is not None:
            self.managed[identity] = roles
        current_roles = self.managed.get(identity, frozenset())
        stored = StoredUser(
            id=old.id if old is not None else UUID(int=7),
            created_at=old.created_at if old is not None else CREATED_AT,
            identity=identity,
            roles=current_roles,
        )
        self.rows[identity] = stored
        return stored


def _result() -> SignInResult:
    return SignInResult(
        identity=IDENTITY,
        access=AccessGrant(
            roles=frozenset({"read", "admin"}),
            profiles=frozenset({"general", "admin"}),
        ),
        credential=NoUserCredential(code=CredentialAbsence.NOT_DELEGATED),
        renewal=RenewalKind.JWT,
        provider="local",
        managed_roles=frozenset({"read"}),
        preferred_profile="general",
    )


async def test_accept_persists_identity_and_limits_session_to_stored_roles() -> None:
    users = Users()
    service = SessionService(users, Profiles())

    session = await service.accept(_result())

    assert session.principal.id == UUID(int=7)
    assert session.principal.identity == IDENTITY
    assert session.principal.roles == {"read"}
    assert session.principal.profiles == {"general"}
    assert users.rows[IDENTITY].roles == {"read"}


async def test_current_loads_updated_roles_and_preserves_only_granted_profiles() -> None:
    users = Users()
    service = SessionService(users, Profiles())
    initial = await service.accept(_result())
    users.managed[IDENTITY] = frozenset({"admin"})
    users.rows[IDENTITY] = users.rows[IDENTITY].model_copy(
        update={"roles": frozenset({"admin"})}
    )

    current = await service.current(initial.principal)

    assert current.roles == {"admin"}
    assert current.profiles == {"general"}


async def test_stored_refuses_missing_or_reassigned_user() -> None:
    users = Users()
    service = SessionService(users, Profiles())
    session = await service.accept(_result())
    users.rows.clear()

    with pytest.raises(AuthenticationError, match="no longer matches"):
        await service.stored(session.principal)
