from __future__ import annotations

from dataclasses import dataclass
from boba.domain.core.stream import StreamSource


@dataclass(frozen=True)
class AgentRequest:
    """Входные данные для AgentLoop.run()"""

    query: str
    model: str
    max_tokens: int


@dataclass(frozen=True)
class AgentConfig:
    """Настройки AgentLoop"""

    max_iterations: int
    default_model: str
    limit_message: str


@dataclass
class AgentContext:
    """
    Контекст, передаваемый через Pipeline и AgentLoop.
    Мутабельный — стадии и цикл дополняют его
    """

    request: AgentRequest
    config: AgentConfig


class AgentLoop(StreamSource[AgentContext, AgentEvent]):
    """
    
    """

    def __init__(
        self,
        config: AgentConfig,
    ) -> None:
        self._config = config
        self

    def run(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ...
