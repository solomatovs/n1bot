"""Раскрытие секретов в дампе: голые SecretStr, вложенность, дамп by_alias."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic.alias_generators import to_camel

from boba.core.secrets import SecretReveal, SecretRevealing


class Inner(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    api_key: SecretStr


class Aliased(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    base_url: str
    inner_part: Inner
    tokens: list[SecretStr]


class Plain(SecretRevealing):
    password: SecretStr
    nested: dict[str, SecretStr]


class TestSecretReveal:
    def test_revealed_opens_bare_secrets_at_any_depth(self) -> None:
        model = Plain(password=SecretStr("p1"), nested={"a": SecretStr("p2")})

        dump = model.revealed()

        if dump != {"password": "p1", "nested": {"a": "p2"}}:
            raise AssertionError(f"secrets are not revealed: {dump}")

    def test_dump_by_alias_is_revealed_under_the_alias_keys(self) -> None:
        model = Aliased(
            base_url="http://x",
            inner_part=Inner(api_key=SecretStr("k")),
            tokens=[SecretStr("t1"), SecretStr("t2")],
        )

        dump = model.model_dump(
            mode="json", by_alias=True, context={SecretReveal.REVEAL_CONTEXT: True}
        )
        SecretReveal.apply(model, dump)

        expected = {
            "baseUrl": "http://x",
            "innerPart": {"apiKey": "k"},
            "tokens": ["t1", "t2"],
        }
        if dump != expected:
            raise AssertionError(f"alias dump is not revealed: {dump}")
