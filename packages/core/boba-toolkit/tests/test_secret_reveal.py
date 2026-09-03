"""Раскрытие SecretStr в конфиге инструмента: голые поля обходом, явные — сами."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, ClassVar

from pydantic import BaseModel, Field, SecretStr, SerializationInfo, field_serializer

from boba.toolkit.entry import ToolAddress, ToolArgv
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TextResult, ToolResult, pack_result
from boba.toolkit.types import SecretRevealing

MASK = "**********"


class Leaf(BaseModel):
    """Вложенная модель без SecretRevealing: секрет раскрывается обходом снаружи."""

    token: SecretStr


class AlwaysMasked(BaseModel):
    """Поле с явной политикой: пароль не уезжает никогда."""

    password: SecretStr

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str:
        return str(value)


class ByContext(BaseModel):
    """Поле с явной политикой инфра-профилей: раскрытие только по контексту."""

    REVEAL: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    password: SecretStr

    @field_serializer("password", when_used="json")
    def _dump_password(self, value: SecretStr, info: SerializationInfo) -> str | None:
        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(self.REVEAL):
            return None

        return value.get_secret_value()


class DeepConfig(SecretRevealing):
    """Секреты на всех уровнях: поле, модель, словарь, список, явные политики."""

    SECTION: ClassVar[str] = "tool.deep"

    api_key: SecretStr
    leaf: Leaf
    by_name: dict[str, SecretStr]
    leaves: list[Leaf]
    masked: AlwaysMasked
    contextual: ByContext
    plain: str = "visible"


def _config() -> DeepConfig:
    return DeepConfig(
        api_key=SecretStr("key-secret-value"),
        leaf=Leaf(token=SecretStr("leaf-secret-value")),
        by_name={"first": SecretStr("dict-secret-value")},
        leaves=[Leaf(token=SecretStr("list-secret-value"))],
        masked=AlwaysMasked(password=SecretStr("never-leaves-value")),
        contextual=ByContext(password=SecretStr("context-secret-value")),
    )


def test_bare_secrets_reveal_at_any_depth() -> None:
    revealed = _config().revealed()

    assert revealed["api_key"] == "key-secret-value"
    assert revealed["leaf"] == {"token": "leaf-secret-value"}
    assert revealed["by_name"] == {"first": "dict-secret-value"}
    assert revealed["leaves"] == [{"token": "list-secret-value"}]
    assert revealed["plain"] == "visible"


def test_explicit_serializers_keep_their_policy() -> None:
    revealed = _config().revealed()

    assert revealed["masked"] == {"password": MASK}
    assert revealed["contextual"] == {"password": "context-secret-value"}


def test_plain_dump_stays_masked() -> None:
    dumped = _config().model_dump(mode="json")

    assert dumped["api_key"] == MASK
    assert dumped["leaf"] == {"token": MASK}
    assert dumped["by_name"] == {"first": MASK}
    assert dumped["contextual"] == {"password": None}


def test_revealed_dump_rebuilds_the_same_model() -> None:
    source = _config()
    rebuilt = DeepConfig.model_validate(source.revealed())

    assert rebuilt.api_key.get_secret_value() == "key-secret-value"
    assert rebuilt.leaf.token.get_secret_value() == "leaf-secret-value"
    assert rebuilt.contextual.password.get_secret_value() == "context-secret-value"
    assert rebuilt.masked.password.get_secret_value() == MASK


@tool
async def deep_echo(
    text: Annotated[str, Field(min_length=1, description="Что вернуть")],
    cfg: Annotated[DeepConfig, Injected],
) -> tuple[str, ToolResult]:
    """Возвращает текст; секрет читается из injected-конфига."""
    return pack_result(TextResult(text=f"{text}|{cfg.api_key.get_secret_value()}"))


def test_render_parse_roundtrip_carries_secret_off_argv() -> None:
    address = ToolAddress.of(deep_echo)
    command = ToolArgv.render(
        address, deep_echo.args_schema, {"text": "hi", "cfg": _config()}
    )

    joined = " ".join(command.argv)
    assert "key-secret-value" not in joined
    assert b"key-secret-value" in command.config

    kwargs = ToolArgv.parse(deep_echo, list(command.argv[4:]), command.config)

    restored = kwargs["cfg"]
    assert isinstance(restored, DeepConfig)
    assert restored.api_key.get_secret_value() == "key-secret-value"
    assert restored.leaf.token.get_secret_value() == "leaf-secret-value"
