"""Пространства пользователей и перенос проверенной личности между адаптерами."""

import pytest
from pydantic import ValidationError

from boba.identity.principal import IdentityRule, UserIdentity
from boba.identity.session import Login
from boba.identity.signin import SignedIn, SignInMetadata


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


def test_identity_and_session_fields_survive_token_metadata_serialization() -> None:
    identity = UserIdentity(realm="corp", login=Login("alice"))
    metadata = SignInMetadata(
        identity=identity,
        roles=frozenset({"read"}),
        sealed_ticket="session-ticket",
        generation="session-generation",
    )
    persisted = SignInMetadata.parse(metadata.render())

    assert persisted.identity_for("corp:alice") == identity
    assert persisted.roles == {"read"}
    assert persisted.sealed_ticket == "session-ticket"
    assert persisted.generation == "session-generation"


def test_a_sign_in_cannot_pair_an_identifier_with_another_identity() -> None:
    metadata = SignInMetadata(
        identity=UserIdentity(realm="corp-a", login=Login("alice"))
    )

    with pytest.raises(ValidationError, match="expected the same user"):
        SignedIn(
            identifier=Login("corp-b:alice"), display_name="Alice", sign_in=metadata
        )


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


def test_malformed_identity_in_metadata_is_not_treated_as_an_old_session() -> None:
    with pytest.raises(ValidationError):
        SignInMetadata.parse({"identity": {}})


def test_aliases_with_different_case_cannot_silently_replace_each_other() -> None:
    with pytest.raises(ValidationError, match="repeat canonical login"):
        IdentityRule.model_validate(
            {"realm": "corp", "aliases": {"Alice": "first", "ALICE": "second"}}
        )
