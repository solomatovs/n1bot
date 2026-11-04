"""GrantTarget: допустимые получатели прав и их идентификаторы."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from boba.access.grants import GrantSubject, GrantTarget


class TestGrantTarget:
    def test_kinds_are_subject_types(self) -> None:
        kinds = {kind.value for kind in GrantSubject}
        if kinds != {"roles", "users"}:
            raise AssertionError("grant subjects must be users and roles")

    def test_user_target(self) -> None:
        target = GrantTarget.user(UUID(int=7))
        if (target.kind, target.id) != (GrantSubject.USERS, UUID(int=7)):
            raise AssertionError("user target must point into users")

    def test_role_target(self) -> None:
        target = GrantTarget.role(UUID(int=3))
        if (target.kind, target.id) != (GrantSubject.ROLES, UUID(int=3)):
            raise AssertionError("role target must point into roles")

    def test_connection_is_not_a_target(self) -> None:
        with pytest.raises(ValidationError):
            GrantTarget.model_validate({"kind": "connections", "id": UUID(int=1)})

    def test_mcp_server_is_not_a_target(self) -> None:
        with pytest.raises(ValidationError):
            GrantTarget.model_validate({"kind": "mcp_servers", "id": UUID(int=1)})

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError, match="kind"):
            GrantTarget.model_validate({"kind": "groups", "id": 1})
