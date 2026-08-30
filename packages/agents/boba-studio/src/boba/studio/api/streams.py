"""Журнал вывода стадий запуска: окна текста по каналам для страницы.

GET /workflow-runs/{run_id}/streams/{call_id}/channels — каналы с записью;
GET /workflow-runs/{run_id}/streams/{call_id}?channel=&offset= — окно от смещения
(живой хвост: страница шлёт end прошлого окна), ?before= — окно перед смещением.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
404 — запуск не пользователя, канал не виден или записи нет.
503 — хранилище workflow недоступно.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, ClassVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from boba.canvas.journal import StreamSlice
from boba.chat.profiles import ChatProfiles
from boba.identity.context import Subject
from boba.studio.api.auth import ApiIdentity, CurrentUser
from boba.studio.api.urls import WorkflowUrl
from boba.toolkit.channels import JournalChannel, JournalChannels, ToolChannel
from boba.toolrun.streams import ToolStreams
from boba.workflow.records import WorkflowError, WorkflowStoreError
from boba.workflow_engine.service import WorkflowService

__all__ = ["ChannelView", "StreamApi"]

ServiceSource = Callable[[], Awaitable[WorkflowService]]


class ChannelView(BaseModel):
    """Канал журнала: имя для запроса окна и подпись для вкладки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    label: str

    @classmethod
    def of(cls, channel: JournalChannel) -> ChannelView:
        return cls(name=channel.value, label=channel.name.lower())


class WindowQuery(BaseModel):
    """Какое окно: канал, от смещения (хвост) либо перед смещением (прокрутка)."""

    model_config = ConfigDict(extra="forbid")

    channel: str = ToolChannel.STDOUT.value
    offset: int = 0
    before: int | None = None
    profile: str | None = None


class StreamApi:
    """Обработчики окон журнала запуска."""

    TAG: ClassVar[str] = "workflows"

    def __init__(self, service: ServiceSource, profiles: ChatProfiles) -> None:
        self._service = service
        self._profiles = profiles

    def mount(self, router: APIRouter) -> None:
        router.add_api_route(
            WorkflowUrl.STREAM_CHANNELS.value,
            self.channels,
            methods=["GET"],
            tags=[self.TAG],
        )
        router.add_api_route(
            WorkflowUrl.STREAM.value, self.window, methods=["GET"], tags=[self.TAG]
        )

    async def channels(
        self,
        run_id: UUID,
        call_id: str,
        current_user: CurrentUser,
        profile: str | None = None,
    ) -> Sequence[ChannelView]:
        subject = await self._owner(current_user, profile, run_id)

        channels = ToolStreams.recorded_channels(subject.user_key, str(run_id), call_id)

        views: list[ChannelView] = []
        for channel in channels:
            views.append(ChannelView.of(channel))

        return views

    async def window(
        self,
        run_id: UUID,
        call_id: str,
        current_user: CurrentUser,
        query: Annotated[WindowQuery, Query()],
    ) -> StreamSlice:
        subject = await self._owner(current_user, query.profile, run_id)

        # служебные каналы вызова наружу не отдаются: только то, что видит панель
        log_channel = JournalChannels.parse_visible(query.channel)
        if log_channel is None:
            raise HTTPException(status_code=404, detail="stream not found")

        if query.before is None:
            found = ToolStreams.recorded_slice(
                subject.user_key, str(run_id), call_id, query.offset, log_channel
            )
        else:
            found = ToolStreams.recorded_slice_before(
                subject.user_key, str(run_id), call_id, query.before, log_channel
            )

        if found is None:
            raise HTTPException(status_code=404, detail="stream not found")

        return found

    async def _owner(
        self, current_user: CurrentUser, profile: str | None, run_id: UUID
    ) -> Subject:
        """Субъект, которому принадлежит запуск; чужой или неизвестный — 404."""
        identity = ApiIdentity.resolve(current_user, profile, self._profiles)

        try:
            service = await self._service()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        try:
            await service.get_run(identity.subject, run_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WorkflowStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return identity.subject
