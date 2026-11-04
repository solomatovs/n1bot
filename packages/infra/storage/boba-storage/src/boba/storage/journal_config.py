"""Настройки записи и ротации журналов вызова инструмента."""

from __future__ import annotations

from typing import ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class StreamJournalConfig(BaseModel):
    """Настройки журнала, который сборка приложения передаёт StreamJournal."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "stream_journal"

    enable: bool = Field(
        default=False,
        description="Писать вывод каждого вызова инструмента в журнал.",
    )

    reserve_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        description=(
            "Резерв места перед новым журналом: старейшие треды вытесняются, "
            "пока свободного меньше; 0 выключает ротацию."
        ),
    )
