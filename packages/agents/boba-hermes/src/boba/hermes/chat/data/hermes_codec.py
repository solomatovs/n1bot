import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from chainlit.element import ElementDict
from chainlit.step import StepDict
from chainlit.types import ThreadDict
from literalai.observability.step import StepType

from boba.hermes.agent.models import HermesMessage, HermesRole, HermesSession


class HermesSessionCodec:
    """Сессия hermes -> тред chainlit."""

    # счётчики hermes: в UI не показываются, помогают при разборе проблем
    _METADATA_FIELDS: ClassVar[tuple[str, ...]] = (
        "model",
        "source",
        "message_count",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "estimated_cost_usd",
        "parent_session_id",
    )

    def to_thread(
        self,
        session: HermesSession,
        user_id: str | None,
        user_identifier: str | None,
        steps: list[StepDict],
        elements: list[ElementDict],
    ) -> ThreadDict:
        """Тред для сайдбара и resume; шаги и элементы приходят готовыми."""
        return ThreadDict(
            id=session.id,
            createdAt=HermesTimestamp.iso(session.started_at),
            name=self._name(session),
            userId=user_id,
            userIdentifier=user_identifier,
            tags=None,
            metadata=self._metadata(session),
            steps=steps,
            elements=elements,
        )

    @staticmethod
    def _name(session: HermesSession) -> str | None:
        """Заголовок сессии; пока его нет — начало первого сообщения."""
        title = (session.title or "").strip()
        if title:
            return title

        preview = (session.preview or "").strip()

        return preview or None

    @classmethod
    def _metadata(cls, session: HermesSession) -> dict[str, Any]:
        """Только заполненные счётчики: пустые ключи в UI не нужны."""
        metadata: dict[str, Any] = {}
        for name in cls._METADATA_FIELDS:
            value = getattr(session, name)
            if value is not None:
                metadata[name] = value

        return metadata


class HermesMessageCodec:
    """Сообщения hermes -> шаги chainlit."""

    # системный промпт — снимок конфигурации, в диалоге его показывать нечего
    _STEP_TYPES: ClassVar[dict[HermesRole, StepType]] = {
        HermesRole.USER: "user_message",
        HermesRole.ASSISTANT: "assistant_message",
        HermesRole.TOOL: "tool",
    }
    # части мультимодального контента, несущие текст. api_server нормализует
    # входящие в "text", но в истории бывают и остальные: её пишет не только он
    _TEXT_PARTS: ClassVar[frozenset[str]] = frozenset(
        {"text", "input_text", "output_text"}
    )
    # рассуждения и служебные поля хода — в metadata, а не в текст ответа
    _METADATA_FIELDS: ClassVar[tuple[str, ...]] = (
        "reasoning",
        "reasoning_content",
        "finish_reason",
        "token_count",
    )

    def to_steps(
        self, session_id: str, messages: list[HermesMessage]
    ) -> list[StepDict]:
        """История сессии в порядке, в котором её отдал hermes."""
        by_tool_call = self._assistant_by_tool_call(messages)

        steps: list[StepDict] = []
        for message in messages:
            step = self.to_step(session_id, message, by_tool_call)
            if step is not None:
                steps.append(step)

        return steps

    def to_step(
        self,
        session_id: str,
        message: HermesMessage,
        by_tool_call: dict[str, str] | None = None,
    ) -> StepDict | None:
        """Один шаг; None для ролей, которых в UI быть не должно."""
        role = message.role_of()
        if role is None:
            return None

        step_type = self._STEP_TYPES.get(role)
        if step_type is None:
            return None

        created = HermesTimestamp.iso(message.timestamp)
        step = StepDict(
            # id сообщения у hermes — автоинкремент, chainlit ждёт строку
            id=str(message.id),
            # тред задаётся вызывающим: по нему фронт связывает шаг с оценками
            # и элементами, и пустым он остаться не может
            threadId=session_id,
            type=step_type,
            name=message.step_name(),
            output=self._text(message.content),
            input="",
            createdAt=created,
            start=created,
            end=created,
            metadata=self._metadata(message),
        )

        parent = self._parent(message, by_tool_call or {})
        if parent is not None:
            step["parentId"] = parent

        return step

    @classmethod
    def _text(cls, content: str | list[Any] | dict[str, Any] | None) -> str:
        """
        Текст шага: у сообщения с вызовом инструмента его нет, а
        мультимодальное приходит списком частей
        """
        if content is None:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = [cls._part_text(part) for part in content]

            return "\n".join(part for part in parts if part)

        return json.dumps(content, ensure_ascii=False)

    @classmethod
    def _part_text(cls, part: Any) -> str:
        """Текст одной части; картинки уносит HermesImageCodec в элементы."""
        if isinstance(part, str):
            # голую строку в списке api_server сам приводит к текстовой части
            return part

        if not isinstance(part, dict):
            return ""

        if part.get("type") not in cls._TEXT_PARTS:
            return ""

        return str(part.get("text") or "")

    @classmethod
    def _metadata(cls, message: HermesMessage) -> dict[str, Any]:
        """Только заполненные поля хода."""
        metadata: dict[str, Any] = {}
        for name in cls._METADATA_FIELDS:
            value = getattr(message, name)
            if value is not None:
                metadata[name] = value

        return metadata

    @staticmethod
    def _parent(message: HermesMessage, by_tool_call: dict[str, str]) -> str | None:
        """Ответ инструмента вкладывается в вызвавший его шаг ассистента."""
        if not message.tool_call_id:
            return None

        return by_tool_call.get(message.tool_call_id)

    @staticmethod
    def _assistant_by_tool_call(messages: list[HermesMessage]) -> dict[str, str]:
        """tool_call_id -> id шага ассистента, который этот вызов запросил."""
        index: dict[str, str] = {}
        for message in messages:
            for call in message.tool_calls:
                # id шага в chainlit — строка, у hermes это автоинкремент
                index[call.id] = str(message.id)

        return index


class HermesImageCodec:
    """Картинки мультимодального сообщения -> элементы chainlit.

    В chainlit изображение живёт не в тексте шага, а отдельным элементом,
    привязанным к шагу через forId. Без этого картинка, которую пользователь
    прислал в чат, при возврате к треду пропадёт.
    """

    # image_url — то, во что api_server нормализует входящие картинки,
    # input_image приходит от других источников истории
    _IMAGE_PARTS: ClassVar[frozenset[str]] = frozenset({"image_url", "input_image"})

    def to_elements(
        self, session_id: str, messages: list[HermesMessage]
    ) -> list[ElementDict]:
        """Все картинки истории в порядке сообщений."""
        elements: list[ElementDict] = []
        for message in messages:
            elements.extend(self.of_message(session_id, message))

        return elements

    def of_message(
        self, session_id: str, message: HermesMessage
    ) -> list[ElementDict]:
        """Картинки одного сообщения; у текстового их нет."""
        if not isinstance(message.content, list):
            return []

        elements: list[ElementDict] = []
        for number, part in enumerate(message.content):
            url = self._url(part)
            if url:
                elements.append(self._element(session_id, message, number, url))

        return elements

    @classmethod
    def _url(cls, part: Any) -> str:
        """Ссылка на картинку; для текстовой части и мусора — пусто."""
        if not isinstance(part, dict) or part.get("type") not in cls._IMAGE_PARTS:
            return ""

        image = part.get("image_url")
        if isinstance(image, dict):
            return str(image.get("url") or "")

        # input_image кладёт ссылку без вложенного объекта
        return str(image or part.get("url") or "")

    @staticmethod
    def _element(
        session_id: str, message: HermesMessage, number: int, url: str
    ) -> ElementDict:
        """Элемент-картинка, привязанный к своему шагу."""
        return ElementDict(
            id=f"{message.id}-image-{number}",
            threadId=session_id,
            forId=str(message.id),
            type="image",
            name=f"image-{number + 1}",
            url=url,
            display="inline",
            size="medium",
            chainlitKey=None,
            objectKey=None,
            path=None,
            page=None,
            props=None,
            mime=None,
            language=None,
            autoPlay=None,
            playerConfig=None,
        )


@dataclass(slots=True)
class HermesHistory:
    """История треда целиком: шаги и прикреплённые к ним элементы.

    chainlit ждёт их плоскими списками в ThreadDict и сшивает по forId.
    """

    steps: list[StepDict] = field(default_factory=list)
    elements: list[ElementDict] = field(default_factory=list)


class HermesHistoryCodec:
    """Сообщения hermes -> история треда.

    Единственный вход для разбора истории: шаг и картинки одного сообщения
    делаются вместе, иначе элементы легко потерять — в StepDict их нет.
    """

    def __init__(
        self,
        messages: HermesMessageCodec | None = None,
        images: HermesImageCodec | None = None,
    ) -> None:
        self._messages = messages or HermesMessageCodec()
        self._images = images or HermesImageCodec()

    def of(self, session_id: str, messages: list[HermesMessage]) -> HermesHistory:
        """Шаги и элементы в порядке, в котором историю отдал hermes."""
        return HermesHistory(
            steps=self._messages.to_steps(session_id, messages),
            elements=self._images.to_elements(session_id, messages),
        )


class HermesTimestamp:
    """Время hermes (unix-секунды) -> iso-8601, как ждёт chainlit."""

    @staticmethod
    def iso(value: Any) -> str:
        """Пустое время не оставляем: chainlit сортирует треды по createdAt."""
        if isinstance(value, int | float):
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat()

        if isinstance(value, str) and value:
            return value

        return datetime.now(tz=UTC).isoformat()
