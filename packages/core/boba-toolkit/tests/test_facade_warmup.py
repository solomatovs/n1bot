"""Объявление прогрева: реестр наполняет @warmup, контракт проверяется сразу."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from boba.toolkit.facade import ToolFacadeError, WarmupHooks, warmup


class Cfg(BaseModel):
    greeting: str


def declare(fn: Any) -> Any:
    """@warmup по неподходящему телу: тип отказа проверяется в рантайме."""
    return warmup(fn)


class TestWarmupDeclaration:
    """Хук объявляет автор инструмента; хост берёт его из реестра."""

    def test_hook_is_registered_with_its_config_model(self) -> None:
        @warmup
        async def warm_probe(cfg: Cfg) -> None:
            """Прогрев теста."""

        hooks = WarmupHooks.of(__name__)
        names = [hook.name for hook in hooks]
        if "warm_probe" not in names:
            raise AssertionError(f"хук не попал в реестр: {names}")

        hook = WarmupHooks.named(__name__, "warm_probe")
        if hook is None:
            raise AssertionError("named() не нашёл объявленный хук")

        if hook.config_model is not Cfg:
            raise AssertionError(f"модель конфига: {hook.config_model}")

    def test_module_without_hooks_has_empty_registry(self) -> None:
        if WarmupHooks.of("boba.toolkit.entry") != ():
            raise AssertionError("модуль без @warmup не должен иметь хуков")

    def test_plain_function_is_refused(self) -> None:
        """Прогрев ждут корутиной: синхронное тело заблокировало бы старт."""

        def not_async(cfg: Cfg) -> None:
            """Не корутина."""

        with pytest.raises(ToolFacadeError, match="coroutine"):
            declare(not_async)

    def test_two_parameters_are_refused(self) -> None:
        async def two_args(cfg: Cfg, extra: int) -> None:
            """Лишний параметр."""

        with pytest.raises(ToolFacadeError, match="exactly one"):
            declare(two_args)

    def test_non_model_config_is_refused(self) -> None:
        async def wrong_config(cfg: int) -> None:
            """Конфиг не модель."""

        with pytest.raises(ToolFacadeError, match="pydantic model"):
            declare(wrong_config)
