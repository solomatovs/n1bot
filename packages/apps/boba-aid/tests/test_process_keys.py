"""Ключи процессов агента и вызывающий по ключу: реестр ключей и serverId
серверов процесса, профиль треда в субъекте и актуальность входа
пользователя — роли и делегированный билет kerberos читаются из живой
сессии в момент вызова."""

from __future__ import annotations

from uuid import uuid4

import pytest
from chainlit_stand import TabSession

from boba.aid.agent.server import ChatCallers, ProcessKeys
from boba.identity.context import DelegatedTicket, NoUserCredential
from boba.identity.errors import AuthenticationError

pytestmark = pytest.mark.anyio


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

    async def test_each_registered_server_gets_its_own_server_id(self) -> None:
        keys = ProcessKeys()
        holder = TabSession().holder(uuid4(), "general")

        servers = keys.register(holder, ["boba_general", "chat"])
        ids = [server.server_id for server in servers]

        assert [server.name for server in servers] == ["boba_general", "chat"]
        assert [server.type for server in servers] == ["acp", "acp"]
        assert len(set(ids)) == 2
        for server in servers:
            registered = keys.holder(server.server_id)
            assert registered is not None
            assert registered.server == server.name
            assert registered.thread_id == holder.thread_id

        keys.revoke(ids[0])
        assert keys.holder(ids[0]) is None
        assert keys.holder(ids[1]) is not None


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
