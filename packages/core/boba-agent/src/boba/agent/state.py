"""StateChannel — типизированный порт state-канала агента."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Generic, Self, TypeVar, cast

from boba.agent.errors import TerminalError
from boba.agent.events import AgentEvent, PersistenceFailed
from boba.llm.models import RequestId

__all__ = [
    "ChannelDuplicateError",
    "ChannelError",
    "ChannelId",
    "ChannelNotFoundError",
    "ChannelRegistry",
    "StateChannel",
]

TChannel = TypeVar("TChannel", bound="StateChannel")
TChannel_co = TypeVar("TChannel_co", bound="StateChannel", covariant=True)


@dataclass(frozen=True)
class ChannelId(Generic[TChannel_co]):
    """Id state-канала: строковое имя + фантомный тип канала."""

    name: str


class StateChannel(ABC):
    """Базовый порт state-канала; конкретный API — у наследников."""

    @classmethod
    @abstractmethod
    def channel_id(cls) -> ChannelId[Self]:
        """Стабильный per-class id канала."""
        ...


class ChannelError(TerminalError[RequestId, AgentEvent], ABC):
    """Базовая ошибка ChannelRegistry — терминальная для агентского цикла."""

    def __init__(self, channel_id: ChannelId[Any], reason: str) -> None:
        self.channel_id = channel_id
        super().__init__(f"ChannelRegistry: канал '{channel_id.name}' {reason}")

    def to_user_feedback(self, request_id: RequestId) -> PersistenceFailed:
        return PersistenceFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
        )


class ChannelNotFoundError(ChannelError):
    """Канал не зарегистрирован в ChannelRegistry."""

    def __init__(self, channel_id: ChannelId[Any]) -> None:
        super().__init__(channel_id, "не зарегистрирован")


class ChannelDuplicateError(ChannelError):
    """Канал с таким id уже зарегистрирован."""

    def __init__(self, channel_id: ChannelId[Any]) -> None:
        super().__init__(channel_id, "уже зарегистрирован")


class ChannelRegistry:
    """Типизированный реестр зарегистрированных state-каналов."""

    def __init__(self, channels: Iterable[StateChannel] = ()) -> None:
        self._channels: dict[str, StateChannel] = {}
        for channel in channels:
            self.register(channel)

    def register(self, channel: StateChannel) -> None:
        """Зарегистрировать канал; ChannelDuplicateError при дубликате id."""
        cid = type(channel).channel_id()
        if cid.name in self._channels:
            raise ChannelDuplicateError(cid)
        self._channels[cid.name] = channel

    def get(self, channel_id: ChannelId[TChannel]) -> TChannel:
        """Получить канал по id; ChannelNotFoundError при отсутствии."""
        channel = self._channels.get(channel_id.name)
        if channel is None:
            raise ChannelNotFoundError(channel_id)
        return cast(TChannel, channel)

    def has(self, channel_id: ChannelId[Any]) -> bool:
        """Проверить регистрацию канала."""
        return channel_id.name in self._channels
