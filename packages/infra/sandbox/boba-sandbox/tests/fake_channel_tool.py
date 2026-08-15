"""Фейковый модуль инструментов для e2e канального запуска в bwrap.

Пишет в stdout и stderr тела, чтобы тест доказал: болтовня не попадает в
конверт tool_result.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Final

from langchain_core.tools import InjectedToolArg, tool
from pydantic import BaseModel, Field, SecretStr

from boba.toolkit.entry import ToolMain
from boba.toolkit.result import TextResult, ToolResult, render_for_llm


class ChannelConfig(BaseModel):
    """Конфиг с секретом для проверки stdin-доставки."""

    SECTION: ClassVar[str] = "tool.fx"

    token: SecretStr

    def revealed(self) -> dict[str, object]:
        return {"token": self.token.get_secret_value()}


class FxDownError(Exception):
    """Ожидаемый отказ."""


class FxErrorKind(StrEnum):
    DOWN = "fx_down"


@tool(response_format="content_and_artifact")
async def fx_echo(
    text: Annotated[str, Field(min_length=1, description="Что вернуть")],
    cfg: Annotated[ChannelConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Печатает болтовню в оба потока и возвращает текст с секретом."""
    print("noise on stdout")
    print("noise on stderr", file=sys.stderr)

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


EXPECTED: Mapping[type[Exception], FxErrorKind] = {
    FxDownError: FxErrorKind.DOWN,
}

TOOLS: Final = ToolMain.toolset(fx_echo)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
