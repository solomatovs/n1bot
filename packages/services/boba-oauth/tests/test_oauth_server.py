"""Токены и печати сервера авторизации служб: поколение сессий снимает все
входы разом, токен несёт то, что вход знает о себе, продление упирается в
потолок сессии; клиент, зарегистрировавшийся сам, остаётся известен после
рестарта и неизвестен службе с другим секретом."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

import pytest
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from boba.auth import SignIns
from boba.auth.proxy import ProxyAssertionVerifier
from boba.identity.api import AuthenticatedUser, PersistedUsers, UsersUpsert
from boba.identity.principal import UserIdentity
from boba.identity.session import Login, SignInProvider
from boba.identity.signin import SignInMetadata
from boba.oauth.server import (
    AuthServer,
    RegisteredClients,
    ResourceTokens,
    SealedValues,
    ServiceTokens,
    TokenClaims,
    TokenHolder,
)
from boba.stand.signin import SignInStand

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class Salt(StrEnum):
    """Соли службы стенда и профиль, выданный вошедшему."""

    TOKEN = "stand-access-token"
    SEALED = "stand-sealed-values"
    PROFILE = "general"


class TestSessionGeneration:
    """Поколение сессий: токены другого поколения сервис не принимает —
    так рестарт процесса или смена generation снимает все входы разом."""

    PUBLIC: str = "http://127.0.0.1:8650"
    SECRET: str = "stand-session-secret"

    def _tokens(self, generation: str, max_sec: int = 3600) -> ServiceTokens:
        return ServiceTokens(
            self.PUBLIC,
            self.SECRET,
            300,
            max_sec,
            generation,
            Salt.TOKEN,
        )

    def _holder(self, started: int) -> TokenHolder:
        sign_in = SignInMetadata(
            provider=SignInProvider.KERBEROS.value,
            identity=UserIdentity(realm="corporate", login=Login("ivanov")),
            roles=frozenset({"dev"}),
            profiles=frozenset({Salt.PROFILE}),
            principal="ivanov@LOSHARA.COM",
            sealed_ticket="sealed-ticket",
        )

        return TokenHolder(
            login="corporate:ivanov",
            sign_in=sign_in,
            started=started,
            resource=f"{self.PUBLIC}/acp",
            user_id=UUID("2bf9ba29-177a-4ddb-8912-e555dd90cf37"),
        )

    def test_token_carries_the_sign_in(self) -> None:
        """Токен несёт то, что вход знает о себе: по нему вызов получает
        делегированный билет входа kerberos."""
        tokens = self._tokens("stand")
        issued = tokens.session(self._holder(int(time.time())), "stand-client")

        access = tokens.read(issued.access_token)
        if access is None or access.scopes != [Salt.PROFILE]:
            raise AssertionError(f"the token opens the granted endpoint: {access}")

        if access.resource != f"{self.PUBLIC}/acp":
            raise AssertionError(f"the token names its endpoint: {access}")

        metadata = SignInMetadata.parse((access.claims or {})["metadata"])
        ticket = metadata.ticket()
        if ticket is None or ticket.sealed != "sealed-ticket":
            raise AssertionError(f"the token carries the sealed ticket: {metadata}")

        refresh = tokens.refreshed(str(issued.refresh_token))
        if refresh is None:
            raise AssertionError("the refresh token is accepted")

        renewed = tokens.renewed(refresh)
        again = tokens.read(renewed.access_token)
        if again is None or again.resource != access.resource:
            raise AssertionError(f"the renewal keeps the endpoint: {again}")

        kept = SignInMetadata.parse(((again and again.claims) or {})["metadata"])
        if kept.ticket() != ticket:
            raise AssertionError(f"the renewal keeps the sign-in: {kept}")

    def test_tokens_of_another_generation_are_rejected(self) -> None:
        before = self._tokens("before-restart")
        after = self._tokens("after-restart")
        issued = before.session(self._holder(int(time.time())), "stand-client")
        refresh = str(issued.refresh_token)

        if before.read(issued.access_token) is None:
            raise AssertionError("the issuing generation accepts its access token")

        if before.refreshed(refresh) is None:
            raise AssertionError("the issuing generation accepts its refresh token")

        if after.read(issued.access_token) is not None:
            raise AssertionError("another generation rejects the access token")

        if after.refreshed(refresh) is not None:
            raise AssertionError("another generation rejects the refresh token")

    def test_renewal_stops_at_the_session_limit(self) -> None:
        tokens = self._tokens("stand", max_sec=600)
        fresh = tokens.session(self._holder(int(time.time()) - 500), "stand-client")
        left = fresh.expires_in
        if left is None or left > 100:
            raise AssertionError(f"the token ends with the session: {left}")

        with pytest.raises(TokenError, match="over its limit"):
            tokens.session(self._holder(int(time.time()) - 601), "stand-client")


class TestRegisteredClients:
    """Клиент, зарегистрировавшийся сам, остаётся известен после рестарта:
    его запись едет в его же идентификаторе, хранить её сервису негде."""

    SECRET: str = "stand-session-secret"

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

        if found is None or found.client_id != issued:
            raise AssertionError(f"another process knows the client: {found}")

        if found.redirect_uris != client.redirect_uris:
            raise AssertionError(f"the registration is kept whole: {found}")

    def test_foreign_identifier_is_unknown(self) -> None:
        client = self._client()
        RegisteredClients(SealedValues("another-secret", Salt.SEALED), {}).register(
            client
        )

        clients = RegisteredClients(SealedValues(self.SECRET, Salt.SEALED), {})

        if clients.find(str(client.client_id)) is not None:
            raise AssertionError("an identifier of another service is not accepted")

        if clients.find("f625f2db-d8a0-4146-958f-94c6bf9ef636") is not None:
            raise AssertionError("a made-up identifier is not accepted")


class RefreshUsers(PersistedUsers, UsersUpsert):
    """Держит строку пользователя, которую сервер читает при обновлении."""

    def __init__(self, user: AuthenticatedUser) -> None:
        self.current: AuthenticatedUser | None = user
        self.upserts = 0

    async def get_user(self, identifier: Login) -> AuthenticatedUser | None:
        if self.current is None:
            return None

        if self.current.identifier != identifier:
            return None

        return self.current

    async def ensure_user(self, signed: object) -> AuthenticatedUser:
        self.upserts += 1
        if self.current is None:
            raise AssertionError("refresh must not create a user")

        return self.current


class TestRefresh:
    """Refresh rechecks DB membership without changing session credentials."""

    CLIENT_ID: str = "stand-client"

    def _client(self) -> OAuthClientInformationFull:
        return OAuthClientInformationFull(
            client_id=self.CLIENT_ID,
            redirect_uris=[AnyUrl("http://127.0.0.1:53999/callback")],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="none",
        )

    def _server(self, users: RefreshUsers) -> AuthServer:
        case = TestSessionGeneration()
        sealed = SealedValues(case.SECRET, Salt.SEALED)
        tokens = case._tokens("stand")

        return AuthServer(
            case.PUBLIC,
            SignIns(None, {}, {}, profiles=SignInStand.profiles()),
            tokens,
            ProxyAssertionVerifier(()),
            RegisteredClients(sealed, {}),
            sealed,
            [f"{case.PUBLIC}/acp"],
            users=users,
        )

    async def _refresh(self) -> tuple[RefreshToken, ServiceTokens]:
        case = TestSessionGeneration()
        tokens = case._tokens("stand")
        issued = tokens.session(case._holder(int(time.time())), self.CLIENT_ID)
        refresh = tokens.refreshed(str(issued.refresh_token))
        if refresh is None:
            raise AssertionError("the issued refresh token is accepted")

        return refresh, tokens

    @pytest.mark.anyio
    async def test_refresh_uses_current_roles_and_preserves_sso_ticket(self) -> None:
        case = TestSessionGeneration()
        holder = case._holder(int(time.time()))
        user = AuthenticatedUser(
            id=holder.user_id,
            identifier=Login(holder.login),
            created_at=CREATED_AT,
            sign_in=holder.sign_in,
        )
        users = RefreshUsers(user)
        server = self._server(users)
        refresh, tokens = await self._refresh()
        users.current = user.model_copy(
            update={"sign_in": SignInMetadata(identity=holder.sign_in.identity)}
        )

        issued = await server.exchange_refresh_token(
            self._client(), refresh, [Salt.PROFILE]
        )

        access = tokens.read(issued.access_token)
        if access is None:
            raise AssertionError(
                "the reduced refresh still issues a valid access token"
            )

        assert not access.scopes
        claims = TokenClaims.model_validate(access.claims)
        assert not claims.roles
        metadata = SignInMetadata.parse(claims.metadata)
        assert metadata.ticket() == holder.sign_in.ticket()
        assert users.upserts == 0
        old = tokens.refresh_holder(refresh)
        renewed = tokens.refreshed(str(issued.refresh_token))
        if renewed is None:
            raise AssertionError("the refreshed session token is accepted")

        current = tokens.refresh_holder(renewed)
        assert current.started == old.started
        assert current.sign_in.ticket() == old.sign_in.ticket()

    @pytest.mark.anyio
    async def test_refresh_rejects_a_replaced_user_uuid(self) -> None:
        case = TestSessionGeneration()
        holder = case._holder(int(time.time()))
        user = AuthenticatedUser(
            id=holder.user_id,
            identifier=Login(holder.login),
            created_at=CREATED_AT,
            sign_in=holder.sign_in,
        )
        users = RefreshUsers(user)
        server = self._server(users)
        refresh, _ = await self._refresh()
        users.current = user.model_copy(update={"id": UUID(int=9999)})

        with pytest.raises(TokenError, match="user has changed"):
            await server.exchange_refresh_token(self._client(), refresh, [Salt.PROFILE])


@pytest.mark.anyio
async def test_resource_uses_signed_scopes_without_losing_the_sso_ticket() -> (
    None
):
    case = TestSessionGeneration()
    tokens = case._tokens("stand")
    holder = case._holder(int(time.time()))
    issued = tokens.session(holder, "stand-client")
    verifier = ResourceTokens(tokens, holder.resource, [Salt.PROFILE])
    accepted = await verifier.verify_token(issued.access_token)
    assert accepted is not None
    assert accepted.scopes == [Salt.PROFILE]
    claims = TokenClaims.model_validate(accepted.claims)
    assert claims.roles == holder.sign_in.roles
    assert SignInMetadata.parse(claims.metadata).sealed_ticket == "sealed-ticket"
    repeated = await verifier.verify_token(issued.access_token)
    assert repeated is not None
    assert repeated.scopes == [Salt.PROFILE]
