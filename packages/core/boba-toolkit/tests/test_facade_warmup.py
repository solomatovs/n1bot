"""Объявление прогрева: @warmup отдаёт хук, контракт проверяется сразу."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from boba.toolkit.facade import ToolFacadeError, warmup


class Cfg(BaseModel):
    greeting: str


def declare(fn: Any) -> Any:
    """@warmup по неподходящему телу: тип отказа проверяется в рантайме."""
    return warmup(fn)


class TestWarmupDeclaration:
    """Хук объявляет автор инструмента; объявление плагина его перечисляет."""

    @pytest.mark.anyio
    async def test_hook_runs_with_its_config_model(self) -> None:
        seen: list[str] = []

        @warmup
        async def warm_probe(cfg: Cfg) -> None:
            """Прогрев теста."""
            seen.append(cfg.greeting)

        if warm_probe.config_model is not Cfg:
            raise AssertionError(f"модель конфига: {warm_probe.config_model}")
        if warm_probe.name != "warm_probe":
            raise AssertionError(f"имя хука: {warm_probe.name}")

        await warm_probe.run({"greeting": "privet", "enable": True})

        if seen != ["privet"]:
            raise AssertionError(f"прогрев получил секцию своей моделью: {seen}")

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
