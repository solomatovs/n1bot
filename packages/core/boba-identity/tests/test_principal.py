"""Пространства пользователей и перенос проверенной личности между адаптерами."""

import pytest
from pydantic import ValidationError

from boba.identity.principal import IdentityRule, UserIdentity
from boba.identity.session import Login


def test_same_login_in_independent_realms_has_independent_identifiers() -> None:
    identifiers = set()
    for realm in ("corp-a", "corp-b", "aid-local", "mcp-local"):
        identity = UserIdentity(realm=realm, login=Login(" Alice "))
        identifiers.add(identity.identifier)

    assert identifiers == {
        "corp-a:alice",
        "corp-b:alice",
        "aid-local:alice",
        "mcp-local:alice",
    }


def test_explicit_rules_link_different_backend_names_to_one_identifier() -> None:
    directory = IdentityRule(realm="corp", aliases={Login("a.smith"): Login("alice")})
    partner = IdentityRule(realm="corp", aliases={Login("alice.smith"): Login("alice")})

    assert directory.identity(Login("A.Smith")) == partner.identity(
        Login("Alice.Smith")
    )
    assert directory.identity(Login("a.smith")).identifier == "corp:alice"


def test_identity_is_a_small_serializable_domain_value() -> None:
    identity = UserIdentity(realm="corp", login=Login(" Alice "))

    assert identity.model_dump(mode="json") == {"realm": "corp", "login": "alice"}
    assert UserIdentity.model_validate(identity.model_dump()) == identity


@pytest.mark.parametrize(
    "value", ["alice", ":alice", "corp:", "CORP:alice", "corp/a:alice"]
)
def test_identity_identifier_requires_a_valid_explicit_realm(value: str) -> None:
    with pytest.raises(ValidationError):
        UserIdentity.model_validate(value)


def test_aliases_cannot_depend_on_another_alias() -> None:
    with pytest.raises(ValidationError, match="another alias"):
        IdentityRule(
            realm="corp",
            aliases={Login("first"): Login("second"), Login("second"): Login("third")},
        )


def test_malformed_identity_is_rejected() -> None:
    with pytest.raises(ValidationError):
        UserIdentity.model_validate({"realm": "corp", "login": ""})


def test_aliases_with_different_case_cannot_silently_replace_each_other() -> None:
    with pytest.raises(ValidationError, match="repeat canonical login"):
        IdentityRule.model_validate(
            {"realm": "corp", "aliases": {"Alice": "first", "ALICE": "second"}}
        )
