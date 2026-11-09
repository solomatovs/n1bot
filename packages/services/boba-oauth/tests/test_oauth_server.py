"""OAuth service tokens, stateless client registration and refresh behavior."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

import httpx
import pytest
from fastmcp.server.auth import AccessToken
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import JWT_BEARER_GRANT_TYPE, OAuthClientInformationFull
from pydantic import AnyUrl, SecretStr
from starlette.applications import Starlette

from boba.auth import SessionService, SignIns
from boba.auth.config import (
    HeaderProfilesConfig,
    HeaderRolesConfig,
    LocalRolesConfig,
    ProxyAuthConfig,
    ProxyHeaders,
    ProxyProfileProviders,
    ProxyRoleProviders,
)
from boba.auth.providers.proxy import ProxySignature
from boba.identity.admission import RoleMappingConfig
from boba.identity.api import StoredUser, UserRows
from boba.identity.authentication import AccessGrant, AuthenticatedPrincipal
from boba.identity.context import DelegatedTicket
from boba.identity.lifetime import SessionLifetime
from boba.identity.principal import UserIdentity
from boba.identity.signin import ProxyRequest
from boba.oauth.assertions import ProxyAssertions
from boba.oauth.claims import TokenClaims
from boba.oauth.server.clients import RegisteredClients, TrustedClient
from boba.oauth.server.provider import AuthServer
from boba.oauth.server.resources import ResourceTokens, ServiceAuth
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
            (),
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


class ProxyStand:
    """Настоящие auth/HTTP-обработчики службы, без БД и внешних серверов."""

    SECRET = "stand-proxy-signing-secret-for-tests"
    CLIENT_SECRET = "stand-client-secret"
    CLIENT_ID = "aid"
    PEER = "192.0.2.10"

    def __init__(
        self, prefix: str, *, enabled: bool = True, resource_path: str = "/api"
    ) -> None:
        self.public = f"https://service.example{prefix}"
        self.resource = f"{self.public}{resource_path}"
        self.config = ProxyAuthConfig(
            path="/trusted/proxy",
            secret=SecretStr(self.SECRET),
            headers=ProxyHeaders(
                user="X-Identity", timestamp="X-Time", signature="X-Signature"
            ),
            allowed_clients=["192.0.2.0/24"],
            roles=ProxyRoleProviders(
                local=LocalRolesConfig(mapping=RoleMappingConfig({"alice": ["read"]})),
                header=HeaderRolesConfig(name="X-Group-List"),
            ),
            profiles=ProxyProfileProviders(
                header=HeaderProfilesConfig(
                    name="X-Profile-List", selected="X-Selected-Profile"
                )
            ),
        )
        # Роль read сама не даёт профиль: успешный тест доказывает чтение заголовка.
        profiles = SignInStand.profiles(roles=["dev"])
        configs = [self.config] if enabled else []
        sign_ins = SignInStand.backends(profiles=profiles).build(configs)
        self.users = Users(TestSessionGeneration()._stored_user())
        self.tokens = ServiceTokens(
            self.public,
            "session-secret",
            SessionLifetime(ttl_sec=300, max_sec=3600),
            "stand",
            Salt.TOKEN,
        )
        sealed = SealedValues("session-secret", Salt.SEALED)
        self.server = AuthServer(
            self.public,
            sign_ins,
            self.tokens,
            configs,
            RegisteredClients(
                sealed,
                {self.CLIENT_ID: TrustedClient(secret=SecretStr(self.CLIENT_SECRET))},
            ),
            sealed,
            [self.resource],
            SessionService(self.users, profiles),
        )
        auth = ServiceAuth(
            self.public, self.server, self.tokens, "stand", {"api": self.resource}
        )
        self.app = Starlette(routes=auth.app_routes([], []))

    def client(self, peer: str = PEER) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(peer, 1234)),
            base_url=self.public,
        )

    def headers(self, **changes: str) -> dict[str, str]:
        request = ProxyRequest(
            login="Alice",
            timestamp=str(int(time.time())),
            roles="wrt,read",
            profiles=SignInStand.PROFILE,
            profile=SignInStand.PROFILE,
        ).model_copy(update=changes)
        return {
            "X-Identity": request.login,
            "X-Time": request.timestamp,
            "X-Signature": ProxySignature(self.SECRET).sign(request),
            "X-Group-List": request.roles,
            "X-Profile-List": request.profiles,
            "X-Selected-Profile": request.profile,
        }

    def form(self) -> dict[str, str]:
        return {
            "client_id": self.CLIENT_ID,
            "client_secret": self.CLIENT_SECRET,
            "resource": self.resource,
        }

    @property
    def url(self) -> str:
        return f"{self.public}{self.config.path}"


@pytest.fixture(params=["", "/mcp", "/agent"])
def proxy(request: pytest.FixtureRequest) -> ProxyStand:
    return ProxyStand(request.param)


async def test_proxy_headers_issue_scoped_token_with_roles_and_profiles(
    proxy: ProxyStand,
) -> None:
    async with proxy.client() as client:
        response = await client.post(
            proxy.url, headers=proxy.headers(), data=proxy.form()
        )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    issued = response.json()
    assert issued["token_type"] == "Bearer"
    assert "refresh_token" not in issued
    verifier = ResourceTokens(proxy.tokens, proxy.resource, [SignInStand.PROFILE])
    access = await verifier.verify_token(issued["access_token"])
    assert access is not None
    claims = TokenClaims.model_validate(access.claims)
    assert claims.identity.login == "alice"
    assert claims.client_id == proxy.CLIENT_ID
    assert claims.roles == {"read", "wrt"}
    assert claims.profiles == {SignInStand.PROFILE}
    assert proxy.users.upserts == 1
    foreign = ResourceTokens(proxy.tokens, f"{proxy.resource}/other", [])
    assert await foreign.verify_token(issued["access_token"]) is None


@pytest.mark.parametrize("path", ["/api", "/api/", "/"])
@pytest.mark.parametrize("entry", ["headers", "assertion"])
async def test_registered_resource_url_is_preserved(path: str, entry: str) -> None:
    stand = ProxyStand("/mcp", resource_path=path)
    form = stand.form()
    headers = {}
    url = stand.url
    if entry == "assertion":
        url = f"{stand.public}/token"
        form.update(
            grant_type=JWT_BEARER_GRANT_TYPE,
            assertion=ProxyAssertions(stand.SECRET, 0).issue(
                stand.CLIENT_ID, "alice", "dev", stand.public, stand.resource
            ),
        )
    else:
        headers = stand.headers()
    async with stand.client() as client:
        response = await client.post(url, headers=headers, data=form)
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    verifier = ResourceTokens(stand.tokens, stand.resource, [SignInStand.PROFILE])
    access = await verifier.verify_token(token)
    assert access is not None
    assert access.resource == stand.resource
    assert access.scopes == [SignInStand.PROFILE]
    foreign = ResourceTokens(stand.tokens, f"{stand.resource}/other", [])
    assert await foreign.verify_token(token) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("X-Identity", "Bob"),
        ("X-Group-List", "admin"),
        ("X-Profile-List", "other"),
        ("X-Selected-Profile", "other"),
        ("X-Signature", "bad"),
        ("X-Time", ""),
    ],
)
async def test_proxy_headers_reject_unsigned_changes(
    proxy: ProxyStand,
    field: str,
    value: str,
) -> None:
    headers = {**proxy.headers(), field: value}
    async with proxy.client() as client:
        response = await client.post(proxy.url, headers=headers, data=proxy.form())
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert proxy.users.upserts == 0


async def test_proxy_headers_reject_expired_signature(proxy: ProxyStand) -> None:
    headers = proxy.headers(timestamp=str(int(time.time()) - 600))
    async with proxy.client() as client:
        response = await client.post(proxy.url, headers=headers, data=proxy.form())
    assert response.status_code == 401
    assert proxy.users.upserts == 0


async def test_proxy_headers_enforce_client_networks(proxy: ProxyStand) -> None:
    async with proxy.client("198.51.100.10") as client:
        response = await client.post(
            proxy.url, headers=proxy.headers(), data=proxy.form()
        )
    assert response.status_code == 403
    assert proxy.users.upserts == 0


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("client_id", "unknown", 401),
        ("client_secret", "wrong", 401),
        ("client_secret", "", 401),
        ("resource", "https://foreign.example/api", 400),
        ("resource", "", 400),
    ],
)
async def test_proxy_headers_require_trusted_client_and_known_resource(
    proxy: ProxyStand,
    field: str,
    value: str,
    status: int,
) -> None:
    form = {**proxy.form(), field: value}
    async with proxy.client() as client:
        response = await client.post(proxy.url, headers=proxy.headers(), data=form)
    assert response.status_code == status
    assert proxy.users.upserts == 0


async def test_proxy_headers_reject_dynamic_clients(proxy: ProxyStand) -> None:
    dynamic = TestRegisteredClients()._client()
    await proxy.server.register_client(dynamic)
    form = {**proxy.form(), "client_id": str(dynamic.client_id), "client_secret": ""}
    async with proxy.client() as client:
        response = await client.post(proxy.url, headers=proxy.headers(), data=form)
    assert response.status_code == 400
    assert "not a trusted assertion client" in response.json()["error_description"]
    assert proxy.users.upserts == 0


@pytest.mark.parametrize("field", ["profiles", "profile"])
async def test_proxy_headers_reject_unknown_profiles(
    proxy: ProxyStand,
    field: str,
) -> None:
    async with proxy.client() as client:
        response = await client.post(
            proxy.url, headers=proxy.headers(**{field: "unknown"}), data=proxy.form()
        )
    assert response.status_code == 403
    assert proxy.users.upserts == 0


async def test_proxy_header_route_requires_post(proxy: ProxyStand) -> None:
    async with proxy.client() as client:
        response = await client.get(proxy.url, headers=proxy.headers())
    assert response.status_code == 405


async def test_no_proxy_header_route_without_provider() -> None:
    stand = ProxyStand("/agent", enabled=False)
    async with stand.client() as client:
        response = await client.post(
            stand.url, headers=stand.headers(), data=stand.form()
        )
    assert response.status_code == 404


@pytest.mark.parametrize("key", [ProxyStand.SECRET, "wrong-key"])
async def test_oauth_assertion_exchange_remains_available(
    proxy: ProxyStand,
    key: str,
) -> None:
    assertion = ProxyAssertions(key, 0).issue(
        proxy.CLIENT_ID, "alice", "wrt,read", proxy.public, proxy.resource
    )
    form = {**proxy.form(), "grant_type": JWT_BEARER_GRANT_TYPE, "assertion": assertion}
    async with proxy.client() as client:
        response = await client.post(f"{proxy.public}/token", data=form)
    if key != proxy.SECRET:
        assert response.status_code == 401
        assert proxy.users.upserts == 0
        return
    assert response.status_code == 200, response.text
    access = proxy.tokens.read(response.json()["access_token"])
    assert access is not None
    claims = TokenClaims.model_validate(access.claims)
    assert claims.identity.login == "alice"
    assert claims.roles == {"read", "wrt"}
