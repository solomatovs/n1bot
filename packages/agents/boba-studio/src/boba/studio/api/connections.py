"""Соединения пользователя: общие (по роли) видны, свои (личный грант) — правятся.

Владение = личный грант: строка, выданная пользователю лично, принадлежит ему —
он её создаёт, заменяет и удаляет. Выданная по роли — общая, только для чтения.

Проверка: POST /connections/check {profile} и POST /connections/{id}/check — пробное
соединение; исход всегда 200 с ProbeResult{ok, message, elapsed_ms}.

Правила владения и уникальности имени — у UserConnectionsService; маршруты
разбирают запрос, зовут сервис и переводят его отказы в статусы.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя; соединение общее, а не своё.
404 — соединение не видно пользователю.
409 — имя занято среди видимых пользователю соединений.
422 — в профиле замаскированный секрет из ответа GET вместо настоящего.
503 — секция [connections] выключена или хранилище недоступно.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, SerializeAsAny

from boba.chat.profiles import ChatProfiles
from boba.connection_broker.probe import ConnectionProbe
from boba.connection_broker.service import UserConnectionsService
from boba.connection_broker.store import ConnectionStoreError
from boba.connection_broker.user_connections import CredentialsRef
from boba.connections.manifest import ConnectionTypes, ConnectionTypesError
from boba.connections.marks import ConnectionRefusal
from boba.connections.profile import (
    ConnectionProfileBase,
    ProbeResult,
    StoredConnection,
)
from boba.identity.api import ApiSubject
from boba.identity.context import Scope, Subject
from boba.identity.errors import RefusalError
from boba.identity.locks import LockToken
from boba.messaging import ChangeAction, ConnectionsChanged, MessageBus
from boba.studio.api.auth import CurrentSubject, CurrentUser
from boba.studio.api.urls import ConnectionUrl

__all__ = [
    "ConnectionBody",
    "ConnectionDeleted",
    "ConnectionView",
    "ConnectionsApi",
    "ProbeBody",
    "ProfileSchema",
]


class MaskedSecrets:
    """Замаскированные SecretStr из ответа GET: назад в профиль их принимать нельзя."""

    MASK: ClassVar[str] = "**********"

    @classmethod
    def find(cls, value: Any) -> bool:
        if isinstance(value, SecretStr):
            return value.get_secret_value() == cls.MASK

        if isinstance(value, BaseModel):
            for field in value.__dict__.values():
                if cls.find(field):
                    return True

        return False


BusSource = Callable[[], MessageBus]
"""Шина процесса; зовётся на запрос."""


class ConnectionBody(BaseModel):
    """Имя и сырой профиль соединения: модель выбирает реестр типов на границе."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    profile: Mapping[str, Any]


class ConnectionView(BaseModel):
    """Строка connections для страницы: профиль с замаскированными секретами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    kind: str
    mine: bool
    profile: SerializeAsAny[ConnectionProfileBase]

    @classmethod
    def of(cls, row: StoredConnection, mine: bool) -> ConnectionView:
        return cls(
            id=row.id, name=row.name, kind=row.kind, mine=mine, profile=row.profile
        )


class ProbeBody(BaseModel):
    """Сырой профиль на проверку: как в форме, ещё не сохранённый."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: Mapping[str, Any]


class ConnectionDeleted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deleted: bool


class ProfileSchema:
    """JSON Schema профиля соединения: по ней страница строит форму."""

    @staticmethod
    def render(types: ConnectionTypes) -> Mapping[str, Any]:
        return types.json_schema()


class RefusalStatus:
    """Статус ответа по виду отказа сервиса соединений."""

    _STATUS: ClassVar[Mapping[str, int]] = {
        ConnectionRefusal.NOT_VISIBLE: 404,
        ConnectionRefusal.NOT_OWNED: 403,
        ConnectionRefusal.NAME_TAKEN: 409,
    }

    @classmethod
    def of(cls, exc: RefusalError) -> HTTPException:
        status = cls._STATUS.get(exc.kind)
        if status is None:
            msg = f"unexpected connection refusal {exc.kind!r}: {exc}"
            raise RuntimeError(msg) from exc

        return HTTPException(status_code=status, detail=str(exc))


class ConnectionsApi:
    """Обработчики /connections над сервисом соединений субъекта."""

    TAG: ClassVar[str] = "connections"

    def __init__(
        self,
        service: UserConnectionsService,
        profiles: ChatProfiles,
        credentials: CredentialsRef,
        bus: BusSource,
        types: ConnectionTypes,
    ) -> None:
        self._service = service
        self._profiles = profiles
        self._credentials = credentials
        self._bus = bus
        self._types = types

    def _parsed(self, raw: Mapping[str, Any]) -> ConnectionProfileBase:
        """Профиль из тела запроса: модель по kind, секреты — настоящие."""
        try:
            profile = self._types.parse(raw)
        except ConnectionTypesError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if MaskedSecrets.find(profile):
            msg = "profile carries a masked secret: enter the real value"
            raise HTTPException(status_code=422, detail=msg)

        return profile

    async def _changed(
        self, subject: Subject, connection_id: UUID, name: str, action: ChangeAction
    ) -> None:
        """Сообщает ленте соединений пользователя на всех инстансах об изменении."""
        message = ConnectionsChanged(
            connection_id=connection_id, name=name, action=action
        )
        await self._bus().publish(
            Scope.user(subject.user_id), message, LockToken.local()
        )

    def mount(self, router: APIRouter) -> None:
        routes = (
            (ConnectionUrl.SCHEMA, self.schema, "GET"),
            (ConnectionUrl.CONNECTIONS, self.list_connections, "GET"),
            (ConnectionUrl.CONNECTIONS, self.create, "POST"),
            (ConnectionUrl.CHECK, self.check, "POST"),
            (ConnectionUrl.CONNECTION_CHECK, self.check_stored, "POST"),
            (ConnectionUrl.CONNECTION, self.replace, "PUT"),
            (ConnectionUrl.CONNECTION, self.delete, "DELETE"),
        )
        for path, handler, method in routes:
            router.add_api_route(path.value, handler, methods=[method], tags=[self.TAG])

    async def schema(self, current_user: CurrentUser) -> Mapping[str, Any]:
        """Схема профиля с вариантами по kind и method; секреты — format=password."""

        return ProfileSchema.render(self._types)

    async def list_connections(
        self,
        identity: CurrentSubject,
        kind: str | None = None,
    ) -> Sequence[ConnectionView]:
        kinds = list(self._types.kinds())
        if kind is not None:
            kinds = [kind]

        async with self._served():
            visible = await self._service.visible(identity.subject, kinds)

        views: list[ConnectionView] = []
        for item in visible:
            views.append(ConnectionView.of(item.row, item.mine))

        return views

    async def create(
        self,
        body: ConnectionBody,
        identity: CurrentSubject,
    ) -> ConnectionView:
        subject = identity.subject
        async with self._served():
            row = await self._service.create(subject, body.name, self._parsed(body.profile))

        await self._changed(subject, row.id, row.name, ChangeAction.CREATED)
        return ConnectionView.of(row, mine=True)

    async def replace(
        self,
        connection_id: UUID,
        body: ConnectionBody,
        identity: CurrentSubject,
    ) -> ConnectionView:
        subject = identity.subject
        async with self._served():
            row = await self._service.replace(
                subject, connection_id, body.name, self._parsed(body.profile)
            )

        await self._changed(subject, row.id, row.name, ChangeAction.UPDATED)
        return ConnectionView.of(row, mine=True)

    async def delete(
        self, connection_id: UUID, identity: CurrentSubject
    ) -> ConnectionDeleted:
        subject = identity.subject
        async with self._served():
            outcome = await self._service.delete(subject, connection_id)

        if outcome.deleted:
            await self._changed(
                subject, connection_id, outcome.name, ChangeAction.DELETED
            )

        return ConnectionDeleted(deleted=outcome.deleted)

    async def check(self, body: ProbeBody, identity: CurrentSubject) -> ProbeResult:
        """Пробное соединение по профилю из формы; делегирование — билетом входа."""

        return await self._probed(identity, self._parsed(body.profile))

    async def check_stored(
        self, connection_id: UUID, identity: CurrentSubject
    ) -> ProbeResult:
        """Пробное соединение по сохранённой строке: видимой пользователю."""
        async with self._served():
            row = await self._service.visible_row(identity.subject, connection_id)

        return await self._probed(identity, row.profile)

    @asynccontextmanager
    async def _served(self) -> AsyncGenerator[None, None]:
        """Граница HTTP: отказы сервиса — статусы, недоступность — 503."""
        try:
            yield
        except RefusalError as exc:
            raise RefusalStatus.of(exc) from exc
        except ConnectionStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def _probed(
        self, identity: ApiSubject, profile: ConnectionProfileBase
    ) -> ProbeResult:
        probe = ConnectionProbe(self._credentials(), self._types)

        return await probe.probe(profile, identity.credential)
