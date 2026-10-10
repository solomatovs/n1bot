"""Рендер вложений в текст сообщений: обёртка напоминаний и повторная отрисовка.

Клиент хранит в журнале текст, которым вложение ушло модели (поле rendered),
но в двух случаях рисует вложение заново: queued_command без rendered и
deferred_tools_delta, чей набор объявленных инструментов не совпал с тем, что
едет блоками tool_addition (apt @211718856, i6e @211725275). Компоненты
создаёт Normalizer.

Ошибки: модуль ошибок не выпускает; неизвестный вид вложения помечается в
пояснениях сборки (notes), а не срывает её.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from boba.prompt_assembly.entries import Block, BlockKit, Content, Marker
from boba.prompt_assembly.records import BlockType, JsText, Json



class Reminders:
    """Обёртка <system-reminder>: наложение (fl), снятие (C7), распознавание."""

    def __init__(self) -> None:
        self._js = JsText()

    def wrap(self, text: str) -> str:
        return f"{Marker.REMINDER_OPEN}\n{text}\n{Marker.REMINDER_CLOSE}"

    def keyed_id(self, text: str) -> str | None:
        if not text.startswith(Marker.KEYED_OPEN):
            return None

        start = len(Marker.KEYED_OPEN)
        window = text[start : start + 32 + len(Marker.KEYED_END) + 1]
        end = window.find(Marker.KEYED_END)
        if end == -1:
            return None

        key = window[:end]
        if not self._is_key(key):
            return None

        if not window.startswith(f"{Marker.KEYED_END}\n", end):
            return None

        return key

    def unwrap(self, text: str) -> str:
        key = self.keyed_id(text)
        if key is not None:
            return self._unwrap_keyed(text, key)

        if not text.startswith(Marker.REMINDER_OPEN):
            return text

        if not text.endswith(Marker.REMINDER_CLOSE):
            return text

        start = len(Marker.REMINDER_OPEN)
        if text[start : start + 1] == "\n":
            start += 1

        end = len(text) - len(Marker.REMINDER_CLOSE)
        if end > start and text[end - 1] == "\n":
            end -= 1

        return text[start:end]

    def wrapped(self, text: str) -> bool:
        if text.startswith(Marker.REMINDER_OPEN):
            return True

        return self.keyed_id(text) is not None

    def attribution(self, text: str) -> bool:
        head = f"{Marker.REMINDER_OPEN}\n"
        if not text.startswith(head):
            return False

        body = text[len(head) :]
        if body.startswith(Marker.ATTRIBUTION_ON):
            return True

        return body.startswith(Marker.ATTRIBUTION_OFF)

    def forged(self, text: str) -> bool:
        if not text.startswith(Marker.REMINDER_TAG):
            return False

        following = text[len(Marker.REMINDER_TAG) : len(Marker.REMINDER_TAG) + 1]
        return self._js.breaks_tag(following)

    def escape_forged(self, text: str) -> str:
        if not self.forged(text):
            return text

        return f"&lt;{text[1:]}"

    def _unwrap_keyed(self, text: str, key: str) -> str:
        tail = f"\n{Marker.KEYED_CLOSE}{key}{Marker.KEYED_END}"
        head = len(Marker.KEYED_OPEN) + len(key) + len(Marker.KEYED_END) + 1
        if not text.endswith(tail):
            return text

        if len(text) < head + len(tail):
            return text

        return text[head : len(text) - len(tail)]

    def _is_key(self, key: str) -> bool:
        if len(key) < 16 or len(key) > 32:
            return False

        for char in key:
            if char not in Marker.HEX_DIGITS:
                return False

        return True


class QueuedCommandRenderer:
    """Повторный рендер вложения queued_command, у которого в журнале нет поля rendered.

    Клиент не записывает rendered, когда сообщение пользователя, присланное
    посреди хода, несёт нетекстовые блоки (картинки); тогда текст строится
    заново веткой queued_command функции i6e. Создаётся Normalizer'ом.
    Покрыт только случай человеческого сообщения (commandMode prompt без
    origin); для остальных возвращается None.
    """

    def __init__(self) -> None:
        self._blocks = BlockKit()
        self._reminders = Reminders()

    def render(self, attachment: Mapping[str, Json]) -> list[Content] | None:
        if attachment.get("commandMode") != "prompt":
            return None

        for key in ("origin", "isMeta", "reminderId", "batchedRelayPrompts", "forwardedIntent", "inlinedImagePaths"):
            if attachment.get(key):
                return None

        if attachment.get("renderedByBatchHead"):
            return []

        prompt = attachment.get("prompt")
        if isinstance(prompt, str):
            return [self._reminders.wrap(self._mid_turn(prompt))]

        if not isinstance(prompt, list):
            return None

        texts: list[str] = []
        images: list[Block] = []
        for block in prompt:
            if not isinstance(block, dict):
                return None
            if self._blocks.is_text(block):
                texts.append(self._blocks.text(block))
            if self._blocks.kind(block) == BlockType.IMAGE:
                images.append(block)

        head = self._blocks.text_block(self._reminders.wrap(self._mid_turn("\n".join(texts))))
        return [[head, *images]]

    def _mid_turn(self, text: str) -> str:
        """Xyn."""
        return f"{Marker.MID_TURN_HEAD}{text}{Marker.MID_TURN_TAIL}"


class DeferredToolsText:
    """Повторный рендер текста deferred_tools_delta под другой набор «всплывших» имён.

    В журнале лежит текст, отрендеренный с именами surfacedNames в разделе
    «just became available». Когда запрос собирается с другим набором (режим
    без mid_conv_system, объявление только именами, текст для понижения
    системного сообщения), клиент рендерит вложение заново (apt → i6e).
    Меняются только ведущие разделы, поэтому класс заменяет их в записанном
    тексте, а остальное оставляет как есть. Создаётся Normalizer'ом.
    """

    _SEPARATOR = "\n\n"

    def __init__(self) -> None:
        self._reminders = Reminders()

    def recorded_names(self, attachment: Mapping[str, Json]) -> list[str]:
        """v_n: имена, с которыми текст записан в журнал."""
        return [*self._names(attachment, "surfacedNames"), *self._names(attachment, "replacedNames")]

    def rerender(
        self, attachment: Mapping[str, Json], rendered: Sequence[Content], surfaced: Sequence[str]
    ) -> list[Content] | None:
        if len(rendered) != 1:
            return None

        recorded = rendered[0]
        if not isinstance(recorded, str):
            return None

        body = self._reminders.unwrap(recorded)
        if body == recorded:
            return None

        old_lead = self._SEPARATOR.join(self._lead(attachment, self.recorded_names(attachment)))
        if not body.startswith(old_lead):
            return None

        rest = body[len(old_lead) :]
        if old_lead and rest.startswith(self._SEPARATOR):
            rest = rest[len(self._SEPARATOR) :]

        sections = self._lead(attachment, surfaced)
        if rest:
            sections.append(rest)

        if not sections:
            return []

        return [self._reminders.wrap(self._SEPARATOR.join(sections))]

    def _lead(self, attachment: Mapping[str, Json], surfaced: Sequence[str]) -> list[str]:
        """Ведущие разделы ветки deferred_tools_delta в i6e: всплывшие, обновлённые, отложенные."""
        chosen = set(surfaced)
        replaced = set(self._names(attachment, "replacedNames"))
        fresh: list[str] = []
        updated: list[str] = []
        for name in dict.fromkeys(surfaced):
            if name in replaced:
                updated.append(name)
                continue
            fresh.append(name)

        deferred: list[str] = []
        for line in self._names(attachment, "addedLines"):
            if line not in chosen:
                deferred.append(line)

        sections: list[str] = []
        if fresh:
            sections.append(self._section(Marker.TOOLS_SURFACED, fresh))

        if updated:
            sections.append(self._section(Marker.TOOLS_REPLACED, updated))

        if not deferred:
            return sections

        if not self._names(attachment, "addedNames"):
            return sections

        header = Marker.TOOLS_DEFERRED
        if attachment.get("toolSearchAbsent") is True:
            header = Marker.TOOLS_ADDED

        sections.append(self._section(header, deferred))
        return sections

    def _section(self, header: str, names: Sequence[str]) -> str:
        lines = "\n".join(names)
        return f"{header}\n{lines}"

    def _names(self, attachment: Mapping[str, Json], key: str) -> list[str]:
        value = attachment.get(key)
        if not isinstance(value, list):
            return []

        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(item)

        return names
