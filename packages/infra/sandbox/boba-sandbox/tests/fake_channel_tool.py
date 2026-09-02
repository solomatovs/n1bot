"""Фейковый модуль инструментов для e2e канального запуска в bwrap.

Пишет в stdout и stderr тела, чтобы тест доказал: болтовня не попадает в
конверт tool_result. fx_probe_tmp отдаёт наблюдаемое изнутри состояние
изоляции — им пользуются тесты зиготы.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal

from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool, warmup
from boba.toolkit.ports import Inbound, Outbound
from boba.toolkit.result import TextResult, ToolResult, render_for_llm


class ChannelConfig(BaseModel):
    """Конфиг с секретом для проверки stdin-доставки."""

    SECTION: ClassVar[str] = "tool.fx"

    token: SecretStr

    def revealed(self) -> dict[str, object]:
        return {"token": self.token.get_secret_value()}


class FxWarmupConfig(BaseModel):
    """Конфиг прогрева: значение уезжает в кэш процесса до ready."""

    greeting: str


class WarmCache:
    """Кэш процесса: прогрев кладёт, вызовы (дети форка) читают через COW."""

    value: ClassVar[str] = ""


@warmup
async def warm_cache(cfg: FxWarmupConfig) -> None:
    WarmCache.value = f"warmed:{cfg.greeting}"


class FxDownError(Exception):
    """Ожидаемый отказ."""


class FxErrorKind(StrEnum):
    DOWN = "fx_down"


@tool
async def fx_echo(
    text: Annotated[str, Field(min_length=1, description="Что вернуть")],
    sleep_sec: Annotated[float, Field(ge=0, description="Пауза перед ответом")] = 0,
    *,
    cfg: Annotated[ChannelConfig, Injected],
) -> tuple[str, ToolResult]:
    """Печатает болтовню в оба потока и возвращает текст с секретом."""
    print("noise on stdout")
    print("noise on stderr", file=sys.stderr)

    if text in ("sleepy", "slow"):
        await asyncio.sleep(30)

    if sleep_sec:
        await asyncio.sleep(sleep_sec)

    if text == "boom":
        msg = "fx backend is down"
        raise FxDownError(msg)

    if text == "workspace":
        # запись в смонтированный образ доказывает, что цепочка дала /workspace
        probe = Path("/workspace/fx-probe.txt")
        probe.write_text("written by fx_echo")
        listing = ",".join(sorted(p.name for p in Path("/workspace").iterdir()))
        artifact = TextResult(text=f"workspace:{listing}")
        return render_for_llm(artifact), artifact

    artifact = TextResult(text=f"{text}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


@tool
async def fx_chatter() -> tuple[str, ToolResult]:
    """Пишет в логер и печатает без flush: всё это идёт в stdout процесса."""
    logger = logging.getLogger("fx")
    logger.info("info line from the body")
    logger.warning("warning line from the body")

    print("print line from the body")

    artifact = TextResult(text="chatter")
    return "chatter", artifact


@tool
async def fx_warm_state() -> tuple[str, ToolResult]:
    """Отдаёт содержимое кэша процесса: тёплое — унаследовано от зиготы."""
    artifact = TextResult(text=WarmCache.value)
    return WarmCache.value, artifact


@tool
async def fx_probe_tmp(
    marker: Annotated[str, Field(min_length=1, description="Имя файла-маркера")],
) -> tuple[str, ToolResult]:
    """Пишет маркер в /tmp и отдаёт наблюдаемую изоляцию вызова."""
    own = Path("/tmp") / marker  # noqa: S108
    own.write_text("mine")

    # сосед успевает записать свой маркер: гонка нужна тесту изоляции
    await asyncio.sleep(1.0)

    markers = sorted(p.name for p in Path("/tmp").iterdir())  # noqa: S108

    cap_eff = "?"
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("CapEff:"):
                cap_eff = line.split()[1]

    with open("/proc/sys/user/max_user_namespaces") as sysctl:
        userns_max = sysctl.read().strip()

    with open("/proc/1/comm") as comm:
        init_comm = comm.read().strip()

    state = {
        "markers": markers,
        "pid": os.getpid(),
        "init": init_comm,
        "cap_eff": cap_eff,
        "userns_max": userns_max,
    }
    artifact = TextResult(text=json.dumps(state))
    return json.dumps(state), artifact


class FxChunkHead(BaseModel):
    """Заголовок кадра потокового ответа: номер порции."""

    kind: Literal["chunk"] = "chunk"
    seq: int


class FxDoneHead(BaseModel):
    """Заголовок последнего кадра: сколько порций прошло через тело."""

    kind: Literal["done"] = "done"
    total: int


@tool
async def fx_stream(
    prefix: Annotated[str, Field(description="Приставка к порции")],
    *,
    cfg: Annotated[ChannelConfig, Injected],
    feed: Annotated[Inbound[FxChunkHead], Injected],
    out: Annotated[Outbound[FxChunkHead | FxDoneHead], Injected],
) -> tuple[str, ToolResult]:
    """Отвечает кадром на каждый кадр входа: потоковый вызов в песочнице."""
    total = 0
    for item in feed:
        total += 1
        out.emit(FxChunkHead(seq=total), prefix.encode("utf-8") + item.body)

    out.emit(FxDoneHead(total=total))

    artifact = TextResult(text=f"streamed {total}|{cfg.token.get_secret_value()}")
    return render_for_llm(artifact), artifact


EXPECTED: Mapping[type[Exception], FxErrorKind] = {
    FxDownError: FxErrorKind.DOWN,
}

TOOLS: Final = ToolMain.toolset(
    fx_echo, fx_chatter, fx_probe_tmp, fx_warm_state, fx_stream
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
