"""Реестр провайдеров: конфиг по kind, общий бэкенд на равные секции, отказы."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from boba.llm.chat import (
    ChatEvent,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
)
from boba.llm.embedding import EmbeddingModel
from boba.llm.providers import (
    ChatModelConfig,
    EmbeddingModelConfig,
    LlmBackend,
    LlmProvider,
    LlmProviderManifest,
    LlmProviders,
    LlmProvidersError,
    LlmProviderTypes,
    ProviderRef,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class EchoProvider(LlmProvider):
    kind: Literal["echo"]
    name: str


class EchoChat(ChatModel):
    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        yield ChatReply(content=request.messages[-1].content)


class EchoEmbedding(EmbeddingModel):
    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_documents(
        self, contents: Sequence[str]
    ) -> Sequence[Sequence[float]]:
        vectors: list[Sequence[float]] = []
        for _ in contents:
            vectors.append([0.0] * self._dim)

        return vectors

    async def embed_query(self, content: str) -> Sequence[float]:
        return [0.0] * self._dim

    def dim(self) -> int:
        return self._dim


class EchoBackend(LlmBackend):
    built: int = 0

    def __init__(self, provider: LlmProvider) -> None:
        if not isinstance(provider, EchoProvider):
            raise LlmProvidersError(f"echo backend got kind {provider.kind!r}")

        EchoBackend.built += 1
        self.closed = False

    def chat(self, cfg: ChatModelConfig) -> ChatModel:
        return EchoChat()

    def embedding(self, cfg: EmbeddingModelConfig) -> EmbeddingModel:
        return EchoEmbedding(cfg.dim)

    async def aclose(self) -> None:
        self.closed = True


MANIFEST = LlmProviderManifest(kind="echo", config=EchoProvider, backend=EchoBackend)


class Holder(BaseModel):
    provider: ProviderRef


@pytest.fixture(autouse=True)
def echo_installed(monkeypatch: pytest.MonkeyPatch) -> LlmProviderTypes:
    """Реестр из одного тестового провайдера вместо entry points процесса."""
    types = LlmProviderTypes({"echo": MANIFEST})
    monkeypatch.setattr(LlmProviderTypes, "_installed", types)
    EchoBackend.built = 0

    return types


class TestProviderRef:
    def test_table_is_parsed_by_the_installed_kind(self) -> None:
        holder = Holder.model_validate({"provider": {"kind": "echo", "name": "a"}})

        if not isinstance(holder.provider, EchoProvider):
            raise AssertionError(f"provider: {holder.provider}")
        if holder.provider.name != "a":
            raise AssertionError(holder.provider)

    def test_unknown_kind_names_installed_kinds(self) -> None:
        with pytest.raises(ValidationError, match="installed kinds: \\['echo'\\]"):
            Holder.model_validate({"provider": {"kind": "nope"}})

    def test_dump_keeps_the_provider_fields(self) -> None:
        """Конфиг едет в песочницу json'ом: поля пакета обязаны пережить дамп."""
        holder = Holder.model_validate({"provider": {"kind": "echo", "name": "a"}})

        dumped = holder.model_dump(mode="json")
        if dumped != {"provider": {"kind": "echo", "name": "a"}}:
            raise AssertionError(dumped)

        again = Holder.model_validate(dumped)
        if not isinstance(again.provider, EchoProvider):
            raise AssertionError(again.provider)

    def test_table_without_kind_is_a_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Holder.model_validate({"provider": {"name": "a"}})

    def test_instance_passes_through(self) -> None:
        provider = EchoProvider(kind="echo", name="a")
        holder = Holder.model_validate({"provider": provider})

        if holder.provider is not provider:
            raise AssertionError("готовый экземпляр принимается как есть")


class TestLlmProviders:
    def _chat_cfg(self, name: str) -> ChatModelConfig:
        return ChatModelConfig.model_validate(
            {"provider": {"kind": "echo", "name": name}, "model": "m"}
        )

    async def test_equal_sections_share_one_backend(
        self, echo_installed: LlmProviderTypes
    ) -> None:
        providers = LlmProviders(echo_installed)

        providers.chat(self._chat_cfg("a"))
        providers.chat(self._chat_cfg("a"))
        providers.chat(self._chat_cfg("b"))

        if EchoBackend.built != 2:
            raise AssertionError(f"backends built: {EchoBackend.built}")

    async def test_chat_and_embedding_come_from_the_manifest(
        self, echo_installed: LlmProviderTypes
    ) -> None:
        providers = LlmProviders(echo_installed)

        reply = await providers.chat(self._chat_cfg("a")).reply(
            ChatRequest(messages=[ChatTurn(role=ChatRole.USER, content="hi")])
        )
        embedding = providers.embedding(
            EmbeddingModelConfig.model_validate(
                {
                    "provider": {"kind": "echo", "name": "a"},
                    "model": "e",
                    "dim": 3,
                    "batch_size": 1,
                    "progress_every": 1,
                }
            )
        )

        if not isinstance(reply, ChatReply):
            raise AssertionError(reply)
        if embedding.dim() != 3:
            raise AssertionError(embedding.dim())
        if EchoBackend.built != 1:
            raise AssertionError("один бэкенд на обе способности")

    async def test_aclose_closes_every_backend(
        self, echo_installed: LlmProviderTypes
    ) -> None:
        providers = LlmProviders(echo_installed)
        providers.chat(self._chat_cfg("a"))
        providers.chat(self._chat_cfg("b"))

        await providers.aclose()

        providers.chat(self._chat_cfg("a"))
        if EchoBackend.built != 3:
            raise AssertionError("после aclose бэкенды собираются заново")
