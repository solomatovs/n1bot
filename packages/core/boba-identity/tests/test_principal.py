"""Канонический логин одинаков при любом способе входа."""

import pytest
from pydantic import ValidationError

from boba.identity.principal import IdentityRule, UserIdentity
from boba.identity.session import Login


def test_identity_is_a_normalized_login() -> None:
    identity = UserIdentity.model_validate(" Alice ")
    assert identity.identifier == "alice"
    assert identity == UserIdentity(login=Login("ALICE"))


def test_explicit_rules_link_different_backend_names_to_one_identifier() -> None:
    first = IdentityRule(aliases={Login("a.smith"): Login("alice")})
    second = IdentityRule(aliases={Login("alice.smith"): Login("alice")})
    assert first.identity(Login("A.Smith")) == second.identity(Login("Alice.Smith"))
    assert first.identity(Login("a.smith")).identifier == "alice"


def test_identity_is_a_small_serializable_domain_value() -> None:
    identity = UserIdentity(login=Login(" Alice "))
    assert identity.model_dump(mode="json") == {"login": "alice"}
    assert UserIdentity.model_validate(identity.model_dump()) == identity


@pytest.mark.parametrize(
    "value", ["", "  ", {}, {"login": ""}, {"login": "alice", "source": "other"}]
)
def test_malformed_identity_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        UserIdentity.model_validate(value)


def test_aliases_cannot_depend_on_another_alias() -> None:
    with pytest.raises(ValidationError, match="another alias"):
        IdentityRule(
            aliases={Login("first"): Login("second"), Login("second"): Login("third")}
        )


def test_aliases_with_different_case_cannot_silently_replace_each_other() -> None:
    with pytest.raises(ValidationError, match="repeat canonical login"):
        IdentityRule.model_validate({"aliases": {"Alice": "first", "ALICE": "second"}})
