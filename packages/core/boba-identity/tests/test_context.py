"""Области и субъект: id пользователя — uuid строки users, без производных."""

from uuid import UUID, uuid4

from boba.identity.context import Scope, ScopeKind, Subject


class TestUserScope:
    def test_scope_id_is_the_users_id(self) -> None:
        user_id = uuid4()
        scope = Scope.user(user_id)

        if (scope.kind, scope.id) != (ScopeKind.USER, str(user_id)):
            raise AssertionError(scope)

    def test_distinct_users_get_distinct_scopes(self) -> None:
        if Scope.user(UUID(int=1)) == Scope.user(UUID(int=2)):
            raise AssertionError("scopes must differ per user")


class TestSubjectOfUser:
    def test_keeps_the_users_id_and_renders_its_key(self) -> None:
        user_id = uuid4()
        subject = Subject.of_user(user_id, "ivanov", ("DEV",), "general")

        if subject.user_id != user_id or subject.user_key != str(user_id):
            raise AssertionError(subject)
