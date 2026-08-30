"""Строки users и threads слоя данных чата: uuid-идентификаторы."""

from uuid import UUID

import pytest

from boba.chainlit.data.models import Thread, User


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Чистая логика строк: сессия приложения не нужна."""


class TestUserUuidId:
    """users.id — uuid, который выдаёт строка при создании и который уходит в chainlit."""

    def test_id_is_sent_on_insert(self) -> None:
        columns = User.insert_columns().as_string(None)
        if '"id"' not in columns:
            raise AssertionError("'\"id\"' in columns")
        if "user_uuid" in columns:
            raise AssertionError('"user_uuid" not in columns')

    def test_persisted_id_is_the_uuid(self) -> None:
        user = User(identifier="boba", id=UUID(int=7))
        if user.to_persisted().id != str(UUID(int=7)):
            raise AssertionError("user.to_persisted().id == str(UUID(int=7))")

    def test_id_is_generated(self) -> None:
        if User(identifier="boba").id.version != 4:
            raise AssertionError("a fresh row gets a uuid4 id")

    def test_thread_owner_is_uuid(self) -> None:
        thread = Thread(user_id=UUID(int=7))
        if thread.to_chainlit(None, [], [])["userId"] != str(UUID(int=7)):
            raise AssertionError('thread.to_chainlit(None, [], [])["userId"] == str(UUID(int=7))')

    def test_thread_without_owner(self) -> None:
        if Thread().to_chainlit(None, [], [])["userId"] is not None:
            raise AssertionError('Thread().to_chainlit(None, [], [])["userId"] is None')
