"""Базовые контракты для потоковой обработки.

StreamTransformer — поэлементная трансформация потока (stateful, ABC).
StreamConsumer    — потребление потока целиком (ABC).

Наследники получают проверку pylance при определении класса.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Iterator, TypeVar

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")
TStream = TypeVar("TStream")
TEvent = TypeVar("TEvent")


class StreamTransformer(ABC, Generic[TIn, TOut]):
    """Stateful поэлементная трансформация потока.

    feed() принимает один элемент, yield'ит ноль или более результатов.
    reset() сбрасывает состояние для повторного использования.
    """

    @abstractmethod
    def feed(self, item: TIn) -> Iterator[TOut]: ...

    @abstractmethod
    def reset(self) -> None: ...


class StreamConsumer(ABC, Generic[TStream, TEvent]):
    """Потребитель потока — yield'ит события, накапливает результат.

    consume() итерирует поток, yield'ит промежуточные события.
    После завершения consume() результат доступен через атрибуты.
    """

    @abstractmethod
    def consume(self, stream: TStream) -> Iterator[TEvent]: ...
