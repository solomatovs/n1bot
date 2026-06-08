"""LLMRequestObserver: fan-out on_response + дефолтный no-op."""

from __future__ import annotations

from boba.llm.observer import CompositeLLMRequestObserver, LLMRequestObserver


class _RecordingObserver(LLMRequestObserver[str, str, str, Exception, Exception]):
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.responses: list[str] = []

    def on_request(self, request: str) -> None:
        del request

    def on_response_chunk(self, chunk: str) -> None:
        self.chunks.append(chunk)

    def on_response(self, response: str) -> None:
        self.responses.append(response)

    def on_request_end(self) -> None:
        pass

    def on_request_cancel(self) -> None:
        pass


class _MinimalObserver(LLMRequestObserver[str, str, str, Exception, Exception]):
    """Без переопределения on_response — должен работать как no-op."""

    def on_request(self, request: str) -> None:
        del request

    def on_response_chunk(self, chunk: str) -> None:
        del chunk

    def on_request_end(self) -> None:
        pass

    def on_request_cancel(self) -> None:
        pass


def test_composite_fans_out_on_response() -> None:
    a, b = _RecordingObserver(), _RecordingObserver()
    composite = CompositeLLMRequestObserver([a, b])

    composite.on_response("full")

    assert a.responses == ["full"]
    assert b.responses == ["full"]


def test_composite_fans_out_on_response_chunk() -> None:
    a = _RecordingObserver()
    composite = CompositeLLMRequestObserver([a])

    composite.on_response_chunk("c1")
    composite.on_response_chunk("c2")

    assert a.chunks == ["c1", "c2"]
    assert a.responses == []


def test_on_response_default_noop() -> None:
    # не переопределён -> вызов не падает и ничего не делает
    _MinimalObserver().on_response("ignored")
