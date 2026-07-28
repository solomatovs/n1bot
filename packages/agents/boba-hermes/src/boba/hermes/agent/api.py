import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from httpx import AsyncClient, HTTPError, Response

from boba.hermes.agent.events import HermesEvent, HermesEventStream
from boba.hermes.agent.models import (
    HermesError,
    HermesMessage,
    HermesSession,
    HermesSessionPage,
)
from boba.hermes.errors import ExternalServiceError, InternalServiceError
from boba.hermes.infra.config import HermesConfig

logger = logging.getLogger(__name__)


class HermesHttp:
    """Запросы к api_server: общий адрес, ключ и перевод ошибок в доменные.

    Разделено с клиентом профиля, потому что часть запросов адресована всему
    инстансу (создание профиля), а часть — одному профилю.
    """

    SERVICE: ClassVar[str] = "hermes"
    _NOT_FOUND: ClassVar[int] = 404
    _CONFLICT: ClassVar[int] = 409

    def __init__(
        self, client: AsyncClient, base: str, api_key: str, scope: str
    ) -> None:
        self._client = client
        self._base = base
        self._headers = {"Authorization": f"Bearer {api_key}"}
        # чей это запрос — попадает в текст ошибки для лога
        self._scope = scope

    async def _request(self, method: str, path: str, **kwargs: Any) -> Response:
        try:
            return await self._client.request(
                method, f"{self._base}{path}", headers=self._headers, **kwargs
            )
        except HTTPError as exc:
            logger.warning("hermes %s %s failed: %s", method, path, exc)
            raise ExternalServiceError(
                self.SERVICE, "Agent backend is unavailable"
            ) from exc

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._payload(await self._request(method, path, **kwargs))

    def _payload(self, response: Response) -> dict[str, Any]:
        """Тело успешного ответа; ошибку hermes переводим в доменную."""
        if response.is_success:
            return response.json()
        raise self._failed(response.request.method, response.request.url.path, response)

    def _failed(self, method: str, path: str, response: Response) -> Exception:
        detail = self._error_detail(response)
        logger.warning(
            "hermes %s %s → %s: %s", method, path, response.status_code, detail
        )
        if response.status_code >= 500:  # noqa: PLR2004
            return ExternalServiceError(
                self.SERVICE, f"hermes is unavailable: {detail}"
            )
        # 4xx — наш запрос неверен: ключ, профиль или тело
        return InternalServiceError(
            internal_detail=(
                f"{method} {path} for {self._scope}: {response.status_code} {detail}"
            ),
            user_detail="Agent backend rejected the request",
        )

    @staticmethod
    def _error_detail(response: Response) -> str:
        """Сообщение из конверта ошибки hermes; иначе — сырое тело."""
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]

        error = HermesError.of(body)
        if error is None:
            return str(body)[:200]

        return f"{error.message} ({error.code})" if error.code else error.message


class HermesApiClient(HermesHttp):
    """Клиент api_server hermes, привязанный к одному профилю.

    Все пути уходят под префикс /p/<профиль>/: у профиля своя state.db, поэтому
    чужие сессии в ответах физически не появляются.
    """

    def __init__(
        self,
        client: AsyncClient,
        config: HermesConfig,
        profile: str,
        events: HermesEventStream | None = None,
    ) -> None:
        super().__init__(
            client,
            base=f"{config.base_url.rstrip('/')}/p/{profile}",
            api_key=config.api_key,
            scope=f"profile {profile}",
        )
        self._profile = profile
        self._events = events or HermesEventStream()

    async def list_sessions(self, limit: int, offset: int) -> HermesSessionPage:
        """Страница сессий профиля."""
        payload = await self._json(
            "GET", "/api/sessions", params={"limit": limit, "offset": offset}
        )

        return HermesSessionPage.from_api(payload)

    async def get_session(self, session_id: str) -> HermesSession | None:
        """Сессия или None, если её нет."""
        response = await self._request("GET", f"/api/sessions/{session_id}")
        if response.status_code == self._NOT_FOUND:
            return None

        return self._session_of(self._payload(response))

    async def create_session(self, session_id: str, **fields: Any) -> HermesSession:
        """Создать сессию; если она уже есть, вернуть существующую.

        409 здесь означает, что состояние уже нужное: chainlit заводит тред один
        раз, но on_chat_resume и параллельные вкладки приходят повторно.
        """
        body = {"id": session_id, **{k: v for k, v in fields.items() if v is not None}}
        response = await self._request("POST", "/api/sessions", json=body)
        if response.status_code == self._CONFLICT:
            existing = await self.get_session(session_id)
            if existing is not None:
                return existing
            raise self._failed("POST", "/api/sessions", response)

        return self._session_of(self._payload(response))

    async def update_session(
        self, session_id: str, **fields: Any
    ) -> HermesSession | None:
        """Обновить title/end_reason; None, если сессии уже нет."""
        response = await self._request(
            "PATCH", f"/api/sessions/{session_id}", json=fields
        )
        if response.status_code == self._NOT_FOUND:
            return None

        return self._session_of(self._payload(response))

    async def delete_session(self, session_id: str) -> bool:
        """Удалить сессию; False, если её и так не было."""
        response = await self._request("DELETE", f"/api/sessions/{session_id}")
        if response.status_code == self._NOT_FOUND:
            return False
        self._payload(response)
        return True

    async def get_messages(self, session_id: str) -> list[HermesMessage]:
        """История сессии; пустой список, если сессии нет."""
        response = await self._request("GET", f"/api/sessions/{session_id}/messages")
        if response.status_code == self._NOT_FOUND:
            return []

        rows = self._payload(response).get("data") or []

        return [HermesMessage.from_api(row) for row in rows]

    async def chat_stream(
        self, session_id: str, message: str
    ) -> AsyncIterator[HermesEvent]:
        """Ход агента в сессии: события приходят по мере работы.

        Историю hermes берёт из самой сессии и туда же дописывает ход,
        поэтому передавать сюда прошлые сообщения не нужно.
        """
        path = f"/api/sessions/{session_id}/chat/stream"
        try:
            async with self._client.stream(
                "POST",
                f"{self._base}{path}",
                headers=self._headers,
                json={"message": message},
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise self._failed("POST", path, response)

                async for event in self._events.of(response.aiter_lines()):
                    yield event
        except HTTPError as exc:
            logger.warning("hermes POST %s failed: %s", path, exc)
            raise ExternalServiceError(
                self.SERVICE, "Agent backend is unavailable"
            ) from exc

    @staticmethod
    def _session_of(payload: dict[str, Any]) -> HermesSession:
        """Сессия из конверта {"object": "hermes.session", "session": {...}}."""
        return HermesSession.from_api(payload.get("session") or {})


class HermesApi(HermesHttp):
    """Инстанс hermes: профили и клиенты к ним поверх общего httpx-соединения.

    Профиль у каждого пользователя свой, а пул соединений один на приложение:
    иначе на каждый запрос поднимался бы новый. Запросы самого этого класса
    идут без префикса /p/<профиль>/ — они адресованы инстансу целиком.
    """

    def __init__(self, client: AsyncClient, config: HermesConfig) -> None:
        super().__init__(
            client,
            base=config.base_url.rstrip("/"),
            api_key=config.api_key,
            scope="hermes instance",
        )
        self._config = config

    def of(self, profile: str) -> HermesApiClient:
        """Клиент, работающий под указанным профилем."""
        return HermesApiClient(self._client, self._config, profile)

    async def create_profile(self, profile: str) -> bool:
        """Завести профиль; False, если он уже есть.

        Профиль наследует config.yaml, .env и навыки от донора из конфига:
        без них у него нет ни модели, ни ключа провайдера. 409 значит, что
        состояние уже нужное, и наружу это не ошибка.
        """
        response = await self._request(
            "POST",
            "/api/profiles",
            json={"name": profile, "clone_from": self._config.default_profile},
        )
        if response.status_code == self._CONFLICT:
            return False

        self._payload(response)

        return True

    async def close(self) -> None:
        """Закрыть общее соединение."""
        await self._client.aclose()
