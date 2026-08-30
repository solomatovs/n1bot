"""Строки users и threads сервиса глазами chainlit: uuid-идентификаторы в словарях."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from boba.chainlit.data.data_layer import ThreadDicts
from boba.chat.threads import StoredThread
from boba.identity.api import StoredUser


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Чистая логика словарей: сессия приложения не нужна."""


class TestUserDict:
    def test_persisted_id_is_the_uuid(self) -> None:
        stored = StoredUser(
            id=UUID(int=7), identifier="boba", created_at=datetime.now(UTC), meta={}
        )

        persisted = ThreadDicts.user(stored)

        assert persisted.id == str(UUID(int=7))
        assert persisted.identifier == "boba"


class TestThreadDict:
    def test_thread_owner_is_uuid(self) -> None:
        stored = StoredThread(
            id=UUID(int=1), created_at=datetime.now(UTC), user_id=UUID(int=7)
        )

        thread = ThreadDicts.thread(stored, "boba", [], [])

        assert thread["userId"] == str(UUID(int=7))
        assert thread["userIdentifier"] == "boba"

    def test_thread_without_owner(self) -> None:
        stored = StoredThread(id=UUID(int=1), created_at=datetime.now(UTC))

        thread = ThreadDicts.thread(stored, None, [], [])

        assert thread["userId"] is None
        assert thread["name"] is None
        assert thread["tags"] is None
