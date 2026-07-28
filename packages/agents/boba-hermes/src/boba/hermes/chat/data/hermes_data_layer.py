import logging
from typing import ClassVar
from uuid import UUID

from chainlit.data.base import BaseDataLayer
from chainlit.element import Element, ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

from boba.hermes.agent.api import HermesApi, HermesApiClient
from boba.hermes.chat.data.data_layer import PostgresDataLayer
from boba.hermes.chat.data.hermes_codec import HermesHistoryCodec, HermesSessionCodec
from boba.hermes.chat.data.profiles import HermesProfileRepository

logger = logging.getLogger(__name__)


class HermesDataLayer(BaseDataLayer):
    """История чатов — из hermes, остальное — из postgres.

    Треды и сообщения живут в state.db профиля hermes и читаются по http.
    Пользователи, оценки, вложения и избранное остаются в postgres: в api
    hermes их нет. Связку thread_id -> владелец тоже держит postgres, иначе
    в http-роутах chainlit профиль не определить — там на входе только id.
    """

    # у hermes limit ограничен двумя сотнями
    _MAX_LIMIT: ClassVar[int] = 200

    def __init__(
        self,
        storage: PostgresDataLayer,
        profiles: HermesProfileRepository,
        api: HermesApi,
        sessions: HermesSessionCodec | None = None,
        history: HermesHistoryCodec | None = None,
    ) -> None:
        self._storage = storage
        self._profiles = profiles
        self._api = api
        self._sessions = sessions or HermesSessionCodec()
        self._history = history or HermesHistoryCodec()

    async def setup(self) -> None:
        """Схема postgres: пользователи, связки профилей, оценки, вложения."""
        await self._storage.setup()

    # --- пользователи: целиком postgres, история к ним не привязана ---

    async def get_user(self, identifier: str) -> PersistedUser | None:
        return await self._storage.get_user(identifier)

    async def create_user(self, user: User) -> PersistedUser | None:
        created = await self._storage.create_user(user)
        if created is None:
            return created

        # профиль закрепляется за пользователем при первом входе
        profile = await self._profiles.ensure(UUID(created.id), created.identifier)
        # у hermes профиля с таким именем может ещё не быть: связка в postgres
        # переживает пересоздание тома, а сам профиль — нет
        await self._api.create_profile(profile)

        return created

    # --- треды: список и содержимое из hermes ---

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        """Страница тредов профиля.

        Сервер chainlit сам проставляет filters.userId перед вызовом, поэтому
        профиль известен и без контекста сессии.
        """
        if not filters.userId:
            return self._empty_page()

        client = await self._client_of(UUID(filters.userId))
        if client is None:
            return self._empty_page()

        limit = min(pagination.first, self._MAX_LIMIT)
        offset = self._offset_of(pagination.cursor)
        page = await client.list_sessions(limit=limit, offset=offset)

        identifier = await self._identifier_of(UUID(filters.userId))
        threads = [
            self._sessions.to_thread(
                session,
                user_id=filters.userId,
                user_identifier=identifier,
                steps=[],
                elements=[],
            )
            for session in page.data
        ]
        threads = self._searched(threads, filters.search)

        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=page.has_more,
                startCursor=str(offset),
                endCursor=str(offset + len(page.data)),
            ),
            data=threads,
        )

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        """Тред с историей: сессия и сообщения из hermes, вложения из postgres."""
        user_id = await self._storage.user_id_by_thread(thread_id)
        if user_id is None:
            return None

        client = await self._client_of(UUID(user_id))
        if client is None:
            return None

        session = await client.get_session(thread_id)
        if session is None:
            return None

        messages = await client.get_messages(thread_id)
        history = self._history.of(thread_id, messages)
        stored = await self._storage.get_thread(thread_id)

        return self._sessions.to_thread(
            session,
            user_id=user_id,
            user_identifier=await self._identifier_of(UUID(user_id)),
            steps=history.steps,
            elements=history.elements + self._elements_of(stored),
        )

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Завести тред и обновить заголовок.

        Строка в postgres нужна как индекс владельца: по ней get_thread
        находит профиль. Заголовок уходит в hermes — по нему там работает
        /resume.
        """
        await self._storage.update_thread(thread_id, name, user_id, metadata, tags)

        owner = user_id or await self._storage.user_id_by_thread(thread_id)
        if owner is None:
            return

        client = await self._client_of(UUID(owner))
        if client is None:
            return

        await client.create_session(thread_id, source="api_server")
        if name:
            await self._rename(client, thread_id, name)

    async def delete_thread(self, thread_id: str) -> None:
        """Удалить тред у hermes и связанные с ним записи в postgres."""
        user_id = await self._storage.user_id_by_thread(thread_id)
        if user_id is not None:
            client = await self._client_of(UUID(user_id))
            if client is not None:
                await client.delete_session(thread_id)

        await self._storage.delete_thread(thread_id)

    async def get_thread_author(self, thread_id: str) -> str:
        """Логин владельца треда; запрос к hermes для этого не нужен."""
        return await self._storage.get_thread_author(thread_id)

    # --- шаги: историю пишет сам hermes во время хода ---

    async def create_step(self, step_dict: StepDict) -> None:
        """Ничего не делает: сообщения в историю добавляет hermes."""

    async def update_step(self, step_dict: StepDict) -> None:
        """Только избранное: остальное содержимое шага живёт в hermes."""
        await self._storage.update_step(step_dict)

    async def delete_step(self, step_id: str) -> None:
        """Ничего не делает: у hermes нет удаления отдельного сообщения."""

    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        return await self._storage.get_favorite_steps(user_id)

    # --- оценки и вложения: их у hermes нет ---

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return await self._storage.upsert_feedback(feedback)

    async def delete_feedback(self, feedback_id: str) -> bool:
        return await self._storage.delete_feedback(feedback_id)

    async def create_element(self, element: Element) -> None:
        await self._storage.create_element(element)

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> ElementDict | None:
        return await self._storage.get_element(thread_id, element_id)

    async def delete_element(
        self, element_id: str, thread_id: str | None = None
    ) -> None:
        await self._storage.delete_element(element_id, thread_id)

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        await self._api.close()
        await self._storage.close()

    # --- вспомогательное ---

    async def _client_of(self, user_id: UUID) -> HermesApiClient | None:
        """Клиент под профилем пользователя; None, если профиля ещё нет."""
        profile = await self._profiles.get(user_id)
        if profile is None:
            logger.info("no hermes profile for user %s", user_id)
            return None

        return self._api.of(profile)

    async def _identifier_of(self, user_id: UUID) -> str | None:
        """Логин владельца профиля для подписи треда."""
        return await self._storage.user_identifier(user_id)

    async def _rename(
        self, client: HermesApiClient, thread_id: str, name: str
    ) -> None:
        """Заголовок в hermes уникален, поэтому при конфликте нумеруем.

        Формат "<имя> #N" родной для hermes: resolve_session_by_title считает
        такие варианты одним именем и по /resume отдаёт самый свежий.
        """
        try:
            await client.update_session(thread_id, title=name)
        except Exception:
            numbered = f"{name} #2"
            logger.info("title %r is taken, retrying as %r", name, numbered)
            await client.update_session(thread_id, title=numbered)

    @staticmethod
    def _elements_of(thread: ThreadDict | None) -> list[ElementDict]:
        """Вложения, сохранённые chainlit; история их не содержит."""
        if thread is None:
            return []

        return list(thread.get("elements") or [])

    @staticmethod
    def _offset_of(cursor: str | None) -> int:
        """Курсор chainlit — строка; у hermes постраничность через offset."""
        if not cursor:
            return 0

        try:
            return max(0, int(cursor))
        except ValueError:
            logger.warning("unexpected thread cursor %r, starting over", cursor)
            return 0

    @staticmethod
    def _searched(threads: list[ThreadDict], search: str | None) -> list[ThreadDict]:
        """Поиска по сессиям у hermes нет: фильтруем страницу по имени."""
        if not search:
            return threads

        needle = search.strip().lower()

        return [t for t in threads if needle in (t.get("name") or "").lower()]

    @staticmethod
    def _empty_page() -> PaginatedResponse[ThreadDict]:
        return PaginatedResponse(
            pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
            data=[],
        )
