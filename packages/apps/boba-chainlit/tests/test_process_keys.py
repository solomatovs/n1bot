"""Ключи процессов агента и вызывающий по ключу: реестр ключей, профиль
треда в субъекте и актуальность входа пользователя — роли и делегированный
билет kerberos читаются из живой сессии в момент вызова."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from chainlit_stand import StandTokens

from boba.chainlit.agent.server import ChatCallers, KeyHolder, ProcessKeys
from boba.chainlit.infra.session import ChainlitSession
from boba.identity.context import DelegatedTicket, NoUserCredential
from boba.identity.errors import AuthenticationError
from boba.identity.session import SignInProvider
from boba.identity.signin import SignInMetadata

pytestmark = pytest.mark.anyio


class TabSession:
    """Сессия вкладки в объёме, который читает держатель ключа: объект с
    токеном входа, который обновление сессии заменяет на месте."""

    LOGIN: ClassVar[str] = "ivanov"
    PRINCIPAL: ClassVar[str] = "ivanov@STAND.LOCAL"

    def __init__(self) -> None:
        self.raw = SimpleNamespace(token="")
        self.live = ChainlitSession(self.raw, StandTokens())

    def sign_in(self, roles: frozenset[str], sealed_ticket: str) -> None:
        """Кладёт в сессию токен входа kerberos с ролями и билетом — так же
        токен заменяет обновление сессии."""
        from chainlit.auth.jwt import create_jwt

        metadata = SignInMetadata(
            provider=SignInProvider.KERBEROS.value,
            roles=roles,
            principal=self.PRINCIPAL,
            sealed_ticket=sealed_ticket,
        )
        self.raw.token = create_jwt(StandTokens.user(self.LOGIN, metadata.render()))

    def password_sign_in(self, roles: frozenset[str]) -> None:
        from chainlit.auth.jwt import create_jwt

        metadata = SignInMetadata(provider=SignInProvider.LOCAL.value, roles=roles)
        self.raw.token = create_jwt(StandTokens.user(self.LOGIN, metadata.render()))

    def holder(self, user_id: UUID, profile: str) -> KeyHolder:
        return KeyHolder(
            user_id=user_id,
            login=self.LOGIN,
            thread_id=str(uuid4()),
            profile=profile,
            session=self.live,
        )


class TestProcessKeys:
    async def test_issued_key_names_its_holder_until_revoked(self) -> None:
        keys = ProcessKeys()
        tab = TabSession()
        holder = tab.holder(uuid4(), "search")

        key = keys.issue(holder)

        assert keys.holder(key) is holder
        keys.revoke(key)
        assert keys.holder(key) is None

    async def test_each_issue_gives_a_new_key(self) -> None:
        keys = ProcessKeys()
        holder = TabSession().holder(uuid4(), "general")

        first = keys.issue(holder)
        second = keys.issue(holder)

        assert first != second
        keys.revoke(first)
        assert keys.holder(second) is holder


class TestCallerOfTheKey:
    async def test_subject_takes_the_profile_of_the_thread(
        self, auth_token: str
    ) -> None:
        keys = ProcessKeys()
        tab = TabSession()
        tab.password_sign_in(frozenset({"DEV"}))
        user_id = uuid4()
        key = keys.issue(tab.holder(user_id, "search"))

        caller = ChatCallers(keys).of_key(key)

        assert caller.subject.user_id == user_id
        assert caller.subject.login == TabSession.LOGIN
        assert caller.subject.roles == frozenset({"DEV"})
        assert caller.subject.profile == "search"
        assert isinstance(caller.credential, NoUserCredential)

    async def test_credential_follows_the_renewed_session_token(
        self, auth_token: str
    ) -> None:
        keys = ProcessKeys()
        tab = TabSession()
        tab.sign_in(frozenset({"DEV"}), "sealed-ticket-at-launch")
        key = keys.issue(tab.holder(uuid4(), "general"))
        callers = ChatCallers(keys)
        launched = callers.of_key(key)

        tab.sign_in(frozenset({"DEV", "ADM"}), "sealed-ticket-renewed")
        renewed = callers.of_key(key)

        assert isinstance(launched.credential, DelegatedTicket)
        assert launched.credential.sealed == "sealed-ticket-at-launch"
        assert isinstance(renewed.credential, DelegatedTicket)
        assert renewed.credential.sealed == "sealed-ticket-renewed"
        assert renewed.credential.principal == TabSession.PRINCIPAL
        assert renewed.subject.roles == frozenset({"DEV", "ADM"})

    async def test_unknown_and_revoked_keys_are_refused(self, auth_token: str) -> None:
        keys = ProcessKeys()
        tab = TabSession()
        tab.password_sign_in(frozenset({"DEV"}))
        key = keys.issue(tab.holder(uuid4(), "general"))
        callers = ChatCallers(keys)

        with pytest.raises(AuthenticationError, match="not a key of a running agent"):
            callers.of_key(tab.raw.token)

        keys.revoke(key)
        with pytest.raises(AuthenticationError, match="unknown or revoked"):
            callers.of_key(key)

    async def test_holder_without_a_sign_in_token_is_refused(self) -> None:
        keys = ProcessKeys()
        key = keys.issue(TabSession().holder(uuid4(), "general"))

        with pytest.raises(AuthenticationError, match="no sign-in token"):
            ChatCallers(keys).of_key(key)
