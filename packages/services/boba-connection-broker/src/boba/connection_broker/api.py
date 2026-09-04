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
409 — имя занято среди видимых пользователю соединений; соединение держит
    другой компонент (каталог), удалять нельзя.
422 — в профиле замаскированный секрет из ответа GET вместо настоящего.
503 — секция [connections] выключена или хранилище недоступно.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializeAsAny,
    ValidationError,
)

from boba.connection_broker.probe import ConnectionProbe
from boba.connection_broker.service import UserConnectionsService
from boba.connection_broker.store import ConnectionStoreError
from boba.connection_broker.user_connections import CredentialsRef
from boba.connections.manifest import ConnectionTypes, ConnectionTypesError
from boba.connections.marks import ConnectionRefusal
from boba.connections.profile import (
    ConnectionProfileBase,
    MissingTypeConnection,
    ProbeResult,
    StoredConnection,
)
from boba.identity.api import ApiSubject
from boba.identity.context import Scope, Subject
from boba.identity.errors import RefusalError
from boba.identity.locks import LockToken
from boba.messaging import ChangeAction, ConnectionsChanged, MessageBus

__all__ = [
    "ConnectionBody",
    "ConnectionDeleted",
    "ConnectionUrl",
    "ConnectionView",
    "ConnectionsApi",
    "ProbeBody",
    "ProfileSchema",
    "SubjectResolver",
]


class ConnectionUrl(StrEnum):
    """Соединения пользователя относительно префикса приложения: список,
    схема профилей, проверка, свои — создание, замена, удаление."""

    CONNECTIONS = "/connections"
    SCHEMA = "/connections/schema"
    CHECK = "/connections/check"
    CONNECTION = "/connections/{connection_id}"
    CONNECTION_CHECK = "/connections/{connection_id}/check"


SubjectResolver = Callable[[Request], Awaitable[ApiSubject]]
"""Субъект входа по запросу; отказ входа резолвер поднимает сам (401/403)."""


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
    """Строка connections для страницы: профиль с замаскированными секретами.

    available=False — тип строки не установлен в этом развёртывании: профиля
    нет, страница показывает пометку вместо формы.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    kind: str
    mine: bool
    available: bool = True
    profile: SerializeAsAny[ConnectionProfileBase] | None = None

    @classmethod
    def of(cls, row: StoredConnection, mine: bool) -> ConnectionView:
        return cls(
            id=row.id, name=row.name, kind=row.kind, mine=mine, profile=row.profile
        )

    @classmethod
    def unavailable(cls, row: MissingTypeConnection, mine: bool) -> ConnectionView:
        return cls(id=row.id, name=row.name, kind=row.kind, mine=mine, available=False)


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
        ConnectionRefusal.IN_USE: 409,
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
        subjects: SubjectResolver,
        credentials: CredentialsRef,
        bus: BusSource,
        types: ConnectionTypes,
    ) -> None:
        self._service = service
        self._subjects = subjects
        self._credentials = credentials
        self._bus = bus
        self._types = types

    def _parsed(self, raw: Mapping[str, Any]) -> ConnectionProfileBase:
        """Профиль из тела запроса: модель по kind, секреты — настоящие.

        Ошибки валидации уходят в формате FastAPI (loc/msg/type): страница
        подсвечивает ими конкретные поля формы.
        """
        kind = raw.get("kind")
        if not isinstance(kind, str):
            msg = f"connection profile expects a string 'kind', got {kind!r}"
            raise HTTPException(status_code=422, detail=msg)

        try:
            manifest = self._types.manifest_of(kind)
        except ConnectionTypesError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            profile = manifest.profile.model_validate(raw)
        except ValidationError as exc:
            # include_input=False: в input разобранного тела ездят секреты
            # kind-тег в loc повторяет формат discriminated union: страница
            # спускается по нему к полю формы
            issues: list[dict[str, Any]] = []
            for issue in exc.errors(include_url=False, include_input=False):
                issues.append(
                    {
                        "loc": ["body", "profile", kind, *issue["loc"]],
                        "msg": issue["msg"],
                        "type": issue["type"],
                    }
                )

            raise HTTPException(status_code=422, detail=issues) from None

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

    async def schema(self, request: Request) -> Mapping[str, Any]:
        """Схема профиля с вариантами по kind и method; секреты — format=password."""
        await self._subjects(request)

        return ProfileSchema.render(self._types)

    async def list_connections(
        self,
        request: Request,
        kind: str | None = None,
    ) -> Sequence[ConnectionView]:
        identity = await self._subjects(request)
        if kind is not None:
            async with self._served():
                visible = await self._service.visible(identity.subject, [kind])

            views: list[ConnectionView] = []
            for item in visible:
                views.append(ConnectionView.of(item.row, item.mine))

            return views

        async with self._served():
            found = await self._service.visible_all(identity.subject)

        views = []
        for item in found.rows:
            views.append(ConnectionView.of(item.row, item.mine))

        # строки типов без пакета показываются с пометкой, а не исчезают
        for broken in found.missing:
            views.append(ConnectionView.unavailable(broken.row, broken.mine))

        return views

    async def create(self, body: ConnectionBody, request: Request) -> ConnectionView:
        identity = await self._subjects(request)
        subject = identity.subject
        async with self._served():
            profile = self._parsed(body.profile)
            row = await self._service.create(subject, body.name, profile)

        await self._changed(subject, row.id, row.name, ChangeAction.CREATED)
        return ConnectionView.of(row, mine=True)

    async def replace(
        self, connection_id: UUID, body: ConnectionBody, request: Request
    ) -> ConnectionView:
        identity = await self._subjects(request)
        subject = identity.subject
        async with self._served():
            row = await self._service.replace(
                subject, connection_id, body.name, self._parsed(body.profile)
            )

        await self._changed(subject, row.id, row.name, ChangeAction.UPDATED)
        return ConnectionView.of(row, mine=True)

    async def delete(self, connection_id: UUID, request: Request) -> ConnectionDeleted:
        identity = await self._subjects(request)
        subject = identity.subject
        async with self._served():
            outcome = await self._service.delete(subject, connection_id)

        if outcome.deleted:
            await self._changed(
                subject, connection_id, outcome.name, ChangeAction.DELETED
            )

        return ConnectionDeleted(deleted=outcome.deleted)

    async def check(self, body: ProbeBody, request: Request) -> ProbeResult:
        """Пробное соединение по профилю из формы; делегирование — билетом входа."""
        identity = await self._subjects(request)

        return await self._probed(identity, self._parsed(body.profile))

    async def check_stored(self, connection_id: UUID, request: Request) -> ProbeResult:
        """Пробное соединение по сохранённой строке: видимой пользователю."""
        identity = await self._subjects(request)
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
