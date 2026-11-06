"""OAuth service tokens, stateless client registration and refresh behavior."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

import pytest
from fastmcp.server.auth import AccessToken
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl, SecretStr

from boba.auth import SessionService, SignIns
from boba.identity.api import StoredUser, UserRows
from boba.identity.authentication import AccessGrant, AuthenticatedPrincipal
from boba.identity.context import DelegatedTicket
from boba.identity.lifetime import SessionLifetime
from boba.identity.principal import UserIdentity
from boba.oauth.claims import TokenClaims
from boba.oauth.server.assertions import ProxyAssertionVerifier
from boba.oauth.server.clients import RegisteredClients, TrustedClient
from boba.oauth.server.provider import AuthServer
from boba.oauth.server.resources import ResourceTokens
from boba.oauth.server.sealed import SealedValues
from boba.oauth.server.tokens import ServiceTokens, TokenHolder
from boba.stand.signin import SignInStand

pytestmark = pytest.mark.anyio

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class Salt(StrEnum):
    """Independent keys for service JWTs and sealed OAuth state."""

    TOKEN = "stand-access-token"
    SEALED = "stand-sealed-values"
    PROFILE = "general"


class Users(UserRows):
    """User rows read during refresh; authentication itself remains stateless."""

    def __init__(self, user: StoredUser) -> None:
        self.current = user
        self.upserts = 0

    async def stored(self, identity: UserIdentity) -> StoredUser | None:
        if self.current.identity != identity:
            return None
        return self.current

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        if self.current.id != user_id:
            return None
        return self.current

    async def upsert(
        self,
        identity: UserIdentity,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        self.upserts += 1
        assigned = self.current.roles if roles is None else roles
        self.current = self.current.model_copy(
            update={"identity": identity, "roles": assigned}
        )
        return self.current


class TestSessionGeneration:
    """Service generation, ticket-bearing claims and absolute session lifetime."""

    PUBLIC = "http://127.0.0.1:8650"
    SECRET = "stand-session-secret"
    USER_ID = UUID("2bf9ba29-177a-4ddb-8912-e555dd90cf37")
    LOGIN = UserIdentity.model_validate("ivanov")
    RESOURCE = f"{PUBLIC}/acp"
    CLIENT_ID = "stand-client"

    def _tokens(self, generation: str, max_sec: int = 3600) -> ServiceTokens:
        return ServiceTokens(
            self.PUBLIC,
            self.SECRET,
            SessionLifetime(ttl_sec=300, max_sec=max_sec),
            generation,
            Salt.TOKEN,
        )

    def _holder(self, started: int) -> TokenHolder:
        principal = AuthenticatedPrincipal(
            id=self.USER_ID,
            identity=self.LOGIN,
            access=AccessGrant(
                roles=frozenset({"dev"}), profiles=frozenset({Salt.PROFILE})
            ),
        )
        credential = DelegatedTicket(
            principal="ivanov@LOSHARA.COM", sealed="sealed-ticket"
        )
        return TokenHolder(
            principal=principal,
            credential=credential,
            started=started,
            resource=self.RESOURCE,
        )

    def _stored_user(self, *, roles: frozenset[str] = frozenset({"dev"})) -> StoredUser:
        return StoredUser(
            id=self.USER_ID,
            created_at=CREATED_AT,
            identity=self.LOGIN,
            roles=roles,
        )

    def test_token_carries_identity_access_and_delegated_ticket(self) -> None:
        tokens = self._tokens("stand")
        issued = tokens.session(self._holder(int(time.time())), self.CLIENT_ID)

        access = tokens.read(issued.access_token)
        assert isinstance(access, AccessToken)
        assert access.scopes == [Salt.PROFILE]
        assert access.resource == self.RESOURCE
        claims = TokenClaims.model_validate(access.claims)
        assert claims.identity == self.LOGIN
        assert claims.roles == {"dev"}
        assert claims.credential == DelegatedTicket(
            principal="ivanov@LOSHARA.COM", sealed="sealed-ticket"
        )

        refresh = tokens.refreshed(str(issued.refresh_token))
        assert isinstance(refresh, RefreshToken)
        holder = tokens.refresh_holder(refresh)
        assert holder.credential == claims.credential
        assert holder.started == claims.iat

    def test_tokens_of_another_generation_are_rejected(self) -> None:
        before = self._tokens("before-restart")
        after = self._tokens("after-restart")
        issued = before.session(self._holder(int(time.time())), self.CLIENT_ID)
        refresh_token = str(issued.refresh_token)

        assert before.read(issued.access_token) is not None
        assert before.refreshed(refresh_token) is not None
        assert after.read(issued.access_token) is None
        assert after.refreshed(refresh_token) is None

    def test_renewal_stops_at_the_session_limit(self) -> None:
        tokens = self._tokens("stand", max_sec=600)
        now = int(time.time())
        fresh = tokens.session(self._holder(now - 500), self.CLIENT_ID)
        assert fresh.expires_in is not None
        assert fresh.expires_in <= 100

        with pytest.raises(TokenError, match="session lifetime is exhausted"):
            tokens.session(self._holder(now - 601), self.CLIENT_ID)


class TestRegisteredClients:
    """A DCR client identifier carries its sealed registration across restarts."""

    SECRET = "stand-session-secret"

    def _client(self) -> OAuthClientInformationFull:
        return OAuthClientInformationFull(
            client_id="issued-by-the-handler",
            redirect_uris=[AnyUrl("http://127.0.0.1:53999/callback")],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="none",
            client_name="stand browser client",
        )

    def test_registration_survives_a_restart(self) -> None:
        client = self._client()
        RegisteredClients(SealedValues(self.SECRET, Salt.SEALED), {}).register(client)
        issued = str(client.client_id)

        found = RegisteredClients(SealedValues(self.SECRET, Salt.SEALED), {}).find(
            issued
        )

        assert found is not None
        assert found.client_id == issued
        assert found.redirect_uris == client.redirect_uris

    def test_foreign_identifier_is_unknown(self) -> None:
        client = self._client()
        RegisteredClients(SealedValues("another-secret", Salt.SEALED), {}).register(
            client
        )
        clients = RegisteredClients(SealedValues(self.SECRET, Salt.SEALED), {})

        assert clients.find(str(client.client_id)) is None
        assert clients.find("f625f2db-d8a0-4146-958f-94c6bf9ef636") is None

    def test_dynamic_clients_cannot_submit_identity_assertions(self) -> None:
        clients = RegisteredClients(
            SealedValues(self.SECRET, Salt.SEALED),
            {"trusted": TrustedClient(secret=SecretStr("client-secret"))},
        )
        assert clients.permits("trusted")
        assert not clients.permits("unknown")
        dynamic = self._client()
        clients.register(dynamic)
        assert dynamic.client_id is not None
        assert clients.find(dynamic.client_id) is not None
        assert not clients.permits(dynamic.client_id)


class TestRefresh:
    """Refresh resolves current roles from users and retains sign-in credentials."""

    CLIENT_ID = TestSessionGeneration.CLIENT_ID

    def _client(self) -> OAuthClientInformationFull:
        return OAuthClientInformationFull(
            client_id=self.CLIENT_ID,
            redirect_uris=[AnyUrl("http://127.0.0.1:53999/callback")],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="none",
        )

    def _server(
        self,
        case: TestSessionGeneration,
        tokens: ServiceTokens,
        users: Users,
    ) -> AuthServer:
        sealed = SealedValues(case.SECRET, Salt.SEALED)
        return AuthServer(
            case.PUBLIC,
            SignIns(None, None, None),
            tokens,
            ProxyAssertionVerifier(()),
            RegisteredClients(sealed, {}),
            sealed,
            [case.RESOURCE],
            SessionService(users, SignInStand.profiles()),
        )

    async def _refresh(
        self, case: TestSessionGeneration, tokens: ServiceTokens
    ) -> RefreshToken:
        issued = tokens.session(case._holder(int(time.time())), self.CLIENT_ID)
        refresh = tokens.refreshed(str(issued.refresh_token))
        assert isinstance(refresh, RefreshToken)
        return refresh

    async def test_refresh_uses_current_roles_and_preserves_sso_ticket(self) -> None:
        case = TestSessionGeneration()
        tokens = case._tokens("stand")
        users = Users(case._stored_user(roles=frozenset()))
        server = self._server(case, tokens, users)
        refresh = await self._refresh(case, tokens)

        issued = await server.exchange_refresh_token(
            self._client(), refresh, [Salt.PROFILE]
        )

        access = tokens.read(issued.access_token)
        assert access is not None
        assert access.scopes == []
        claims = TokenClaims.model_validate(access.claims)
        assert claims.roles == frozenset()
        assert claims.credential == DelegatedTicket(
            principal="ivanov@LOSHARA.COM", sealed="sealed-ticket"
        )
        assert users.upserts == 0

        renewed = tokens.refreshed(str(issued.refresh_token))
        assert isinstance(renewed, RefreshToken)
        holder = tokens.refresh_holder(renewed)
        assert holder.credential == claims.credential

    async def test_refresh_rejects_a_replaced_user_uuid(self) -> None:
        case = TestSessionGeneration()
        tokens = case._tokens("stand")
        users = Users(case._stored_user())
        server = self._server(case, tokens, users)
        refresh = await self._refresh(case, tokens)
        users.current = users.current.model_copy(update={"id": UUID(int=9999)})

        with pytest.raises(TokenError, match="session user no longer matches"):
            await server.exchange_refresh_token(self._client(), refresh, [Salt.PROFILE])


async def test_resource_verifies_signed_scopes_and_preserves_sso_ticket() -> None:
    case = TestSessionGeneration()
    tokens = case._tokens("stand")
    holder = case._holder(int(time.time()))
    issued = tokens.session(holder, case.CLIENT_ID)
    verifier = ResourceTokens(tokens, holder.resource, [Salt.PROFILE])

    accepted = await verifier.verify_token(issued.access_token)
    assert accepted is not None
    assert accepted.scopes == [Salt.PROFILE]
    claims = TokenClaims.model_validate(accepted.claims)
    assert claims.roles == holder.principal.roles
    assert claims.credential == holder.credential

    repeated = await verifier.verify_token(issued.access_token)
    assert repeated is not None
    assert repeated.scopes == [Salt.PROFILE]
