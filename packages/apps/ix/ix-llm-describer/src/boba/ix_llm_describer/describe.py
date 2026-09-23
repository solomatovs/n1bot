"""Описание одного объекта моделью: промпт его поверхности, бюджет входа и свёртка.

Что говорить модели, знает владелец поверхности строкой в {schema}.surface_prompt;
сколько текста модель принимает за раз, знает конфиг описателя, потому что это
свойство модели, а не объекта. Отсюда правило одно на всех: материал короче бюджета
уходит одним вызовом, длиннее — режется по структуре markdown, каждый кусок получает
выжимку, и второй вызов сводит выжимки в описание. Размер страницы после этого
перестаёт иметь значение: в базу в любом случае ложится один текст.

Шаблоны выжимки и сведения лежат файлами пакета: они про способ разговора с моделью, а
не про предметную область, и одинаковы для всех поверхностей. Туда же относится правило
ответа — вызовом функции схемы: описатель дописывает его к системному промпту любой
поверхности, чтобы владельцу не приходилось знать контракт ответа. Системный промпт на
обоих проходах берётся из строки поверхности, поэтому модель остаётся в своей роли.

Ошибки:
DescribeError — модель недоступна или ответила не по схеме, файла шаблона нет.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from boba.ix_core.prompts import SurfacePrompt
from boba.llm.chat import LlmError, ToolSpec
from boba.llm.schema import SchemaReply

__all__ = [
    "DescribeError",
    "Described",
    "Description",
    "MarkdownSplit",
    "PackPrompts",
]


class DescribeError(Exception):
    """Объект не удалось описать."""


class PromptFile(StrEnum):
    """Файлы пакета: схема ответа, правило ответа и шаблоны свёртки."""

    SCHEMA = "schema.json"
    ANSWER = "answer.md"
    PART = "part.md"
    REDUCE = "reduce.md"


class PackPrompts:
    """Шаблоны свёртки и схема ответа из каталога prompt/ пакета.

    Шаблон выжимки получает кусок материала вместо {input}, шаблон сведения —
    пронумерованные выжимки вместо {parts}. Схема ответа одна на всё: модель всегда
    возвращает один текст. Правило ответа называет функцию схемы вместо {function} и
    дописывается к системному промпту поверхности.
    """

    def __init__(self, prompt_dir: Path) -> None:
        self._dir = prompt_dir
        self._part = self._text(PromptFile.PART, "{input}")
        self._reduce = self._text(PromptFile.REDUCE, "{parts}")
        raw = self._read(PromptFile.SCHEMA)
        self.schema = ToolSpec.model_validate(json.loads(raw))
        answer = self._text(PromptFile.ANSWER, "{function}")
        self._answer = answer.replace("{function}", self.schema.name)

    def system(self, surface_system: str) -> str:
        """Системный промпт вызова: роль от владельца поверхности и правило ответа."""
        return f"{surface_system}\n\n{self._answer}"

    def part(self, chunk: str) -> str:
        return self._part.replace("{input}", chunk)

    def reduce(self, parts: Sequence[str]) -> str:
        lines: list[str] = []
        for number, text in enumerate(parts, start=1):
            lines.append(f"{number}. {text}")

        return self._reduce.replace("{parts}", "\n\n".join(lines))

    def material(self) -> str:
        """То, что пакет добавляет в отпечаток пары: шаблоны и схема ответа."""
        return "\n".join(
            [
                self._answer,
                self._part,
                self._reduce,
                json.dumps(
                    dict(self.schema.parameters), sort_keys=True, ensure_ascii=False
                ),
            ]
        )

    def _text(self, name: PromptFile, placeholder: str) -> str:
        text = self._read(name).strip()
        if placeholder not in text:
            raise DescribeError(
                f"{self._dir / name}: expected the placeholder {placeholder}, "
                f"got {text[:80]!r}"
            )

        return text

    def _read(self, name: PromptFile) -> str:
        path = self._dir / name
        if not path.is_file():
            raise DescribeError(f"prompt file {path} not found")

        return path.read_text(encoding="utf-8")


class MarkdownSplit:
    """Резак материала под бюджет модели.

    Текст режется по границам структуры: сначала по заголовкам markdown, потом по
    пустым строкам, и только затем, если абзац сам длиннее бюджета, по знакам. Куски
    набираются жадно, поэтому их получается столько, сколько нужно, а не сколько
    заголовков в тексте.
    """

    def __init__(self, budget: int) -> None:
        if budget <= 0:
            raise DescribeError(f"chunking: expected a positive budget, got {budget}")

        self._budget = budget

    def split(self, text: str) -> list[str]:
        if len(text) <= self._budget:
            return [text]

        return self._pack(self._pieces(text))

    def _pieces(self, text: str) -> list[str]:
        pieces: list[str] = []
        for section in re.compile(r"(?m)^(?=#{1,6} )").split(text):
            pieces.extend(self._paragraphs(section))

        return pieces

    def _paragraphs(self, section: str) -> list[str]:
        if len(section) <= self._budget:
            return [section]

        pieces: list[str] = []
        for paragraph in section.split("\n\n"):
            pieces.extend(self._hard(paragraph))

        return pieces

    def _hard(self, paragraph: str) -> list[str]:
        if len(paragraph) <= self._budget:
            return [paragraph]

        pieces: list[str] = []
        start = 0
        while start < len(paragraph):
            pieces.append(paragraph[start : start + self._budget])
            start += self._budget

        return pieces

    def _pack(self, pieces: Sequence[str]) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for piece in pieces:
            if not piece.strip():
                continue

            if current and size + len(piece) > self._budget:
                chunks.append("\n\n".join(current))
                current = []
                size = 0

            current.append(piece)
            size += len(piece) + len("\n\n")

        if current:
            chunks.append("\n\n".join(current))

        return chunks


class Reply(BaseModel):
    """Ответ модели по схеме пакета."""

    description: str = Field(min_length=1)


@dataclass(frozen=True, kw_only=True)
class Described:
    """Итог описания объекта: текст и сколько кусков понадобилось материалу."""

    text: str
    chunks: int


class Description:
    """Описание объекта: один вызов или свёртка, если материал в бюджет не влез."""

    def __init__(self, reply: SchemaReply, pack: PackPrompts, budget: int) -> None:
        self._reply = reply
        self._pack = pack
        self._budget = budget
        self._split = MarkdownSplit(budget)

    async def of(self, prompt: SurfacePrompt, material: str) -> Described:
        chunks = self._split.split(material)
        if len(chunks) == 1:
            text = await self._ask(prompt, prompt.user(material))
            return Described(text=text, chunks=1)

        parts: list[str] = []
        for chunk in chunks:
            parts.append(await self._ask(prompt, self._pack.part(chunk)))

        text = await self._ask(prompt, self._pack.reduce(parts))

        return Described(text=text, chunks=len(chunks))

    async def _ask(self, prompt: SurfacePrompt, user: str) -> str:
        system = self._pack.system(prompt.system_prompt)
        where = f"describe {prompt.surface}/{prompt.aspect}"

        try:
            raw = await self._reply.ask(system, user, self._pack.schema)
        except LlmError as exc:
            raise DescribeError(f"{where}: model call failed: {exc}") from exc

        try:
            reply = Reply.model_validate(raw)
        except ValidationError as exc:
            raise DescribeError(
                f"{where}: reply is not by schema: {dict(raw)!r:.200}: {exc}"
            ) from exc

        return reply.description.strip()
