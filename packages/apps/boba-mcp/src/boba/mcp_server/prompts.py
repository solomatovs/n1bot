"""Промпты endpoint'ов сервиса из файлов conf/prompts/<endpoint>/<имя>.toml.

Файл — один промпт, его имя — имя файла без суффикса. Формат:
description — что промпт делает; [arguments.<имя>] — аргумент с
description и required; [[messages]] — сообщения по порядку с role (user
или assistant) и text. В text аргумент подставляется по `{<имя>}`,
отсутствующий необязательный — пустой строкой; фигурная скобка текстом
пишется удвоенной. Промпты публикуются по стандарту MCP: prompts/list и
prompts/get (FilePrompts).

Ошибки:
PromptFileError — каталог промптов называет endpoint, которого нет, файл не
    читается, не TOML или не по формату (поле, роль, неизвестный аргумент в
    тексте).
"""

from __future__ import annotations

import string
import tomllib
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from fastmcp.exceptions import PromptError
from fastmcp.prompts import Message, Prompt, PromptArgument
from fastmcp.server.providers import Provider
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boba.core.validation import ValidationText

__all__ = [
    "FilePrompt",
    "FilePrompts",
    "PromptFile",
    "PromptFileError",
    "PromptFiles",
]


class PromptFileError(Exception):
    """Файл промпта или каталог промптов не по формату."""


class PromptFileSuffix(StrEnum):
    """Суффикс файла промпта."""

    TOML = ".toml"


class PromptArgumentFile(BaseModel):
    """Секция [arguments.<имя>] файла промпта."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    required: bool


class PromptMessageFile(BaseModel):
    """Сообщение [[messages]] файла промпта: роль и текст с подстановкой
    аргументов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)


class PromptFile(BaseModel):
    """Файл промпта целиком; аргументы в тексте сообщений — только
    объявленные."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    arguments: dict[str, PromptArgumentFile] = Field(default_factory=dict)
    messages: list[PromptMessageFile] = Field(min_length=1)

    @model_validator(mode="after")
    def _declared(self) -> PromptFile:
        formatter = string.Formatter()
        for message in self.messages:
            for _, field, spec, conversion in formatter.parse(message.text):
                if field is None:
                    continue

                if field not in self.arguments:
                    known = sorted(self.arguments)
                    msg = (
                        f"a message text names {{{field}}}, expected only the "
                        f"declared arguments {known}; write a brace as text doubled"
                    )
                    raise ValueError(msg)

                if spec or conversion:
                    msg = (
                        f"a message text formats {{{field}}} with "
                        f"{conversion or ''}{spec or ''}, expected a bare {{{field}}}"
                    )
                    raise ValueError(msg)

        return self


class FilePrompt(Prompt):
    """Промпт файла: объявление для prompts/list и сообщения с подставленными
    аргументами для prompts/get. Создаёт читатель PromptFiles на каждый файл,
    отдаёт провайдер FilePrompts."""

    source: PromptFile

    async def render(self, arguments: dict[str, Any] | None = None) -> list[Message]:
        """Ошибки:
        PromptError — не назван обязательный аргумент или назван лишний.
        """
        given: dict[str, Any] = {}
        if arguments is not None:
            given = arguments

        unknown = sorted(set(given) - set(self.source.arguments))
        if unknown:
            msg = (
                f"prompt {self.name!r} takes the arguments "
                f"{sorted(self.source.arguments)}, got unknown {unknown}"
            )
            raise PromptError(msg)

        values: dict[str, str] = {}
        for name, argument in self.source.arguments.items():
            value = given.get(name)
            if value is None and argument.required:
                msg = f"prompt {self.name!r} requires the argument {name!r}"
                raise PromptError(msg)

            if value is None:
                value = ""

            values[name] = str(value)

        messages: list[Message] = []
        for message in self.source.messages:
            messages.append(Message(message.text.format_map(values), role=message.role))

        return messages


class PromptFiles:
    """Читатель каталога промптов: подкаталог на endpoint, файл на промпт.
    Каталога нет — промптов нет ни у кого. Создаёт вход процесса сервиса и
    отдаёт итог сборке endpoint'ов."""

    def read(
        self, root: Path, endpoints: Collection[str]
    ) -> Mapping[str, Sequence[FilePrompt]]:
        """Промпты endpoint'ов по именам; endpoint без подкаталога — без
        промптов.

        Ошибки:
        PromptFileError — подкаталог называет неизвестный endpoint, файл не
            читается или не по формату.
        """
        if not root.is_dir():
            return {}

        prompts: dict[str, Sequence[FilePrompt]] = {}
        for folder in sorted(root.iterdir()):
            if folder.name not in endpoints:
                msg = (
                    f"prompts {folder}: expected a folder named after an mcp "
                    f"endpoint {sorted(endpoints)}, got {folder.name!r}"
                )
                raise PromptFileError(msg)

            prompts[folder.name] = self._folder(folder)

        return prompts

    def _folder(self, folder: Path) -> Sequence[FilePrompt]:
        prompts: list[FilePrompt] = []
        for path in sorted(folder.iterdir()):
            if path.suffix != PromptFileSuffix.TOML.value:
                msg = (
                    f"prompts {path}: expected a {PromptFileSuffix.TOML.value} "
                    "file of one prompt"
                )
                raise PromptFileError(msg)

            prompts.append(self._prompt(path))

        return prompts

    def _prompt(self, path: Path) -> FilePrompt:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            msg = f"prompts {path}: the file is not read: {exc}"
            raise PromptFileError(msg) from exc
        except tomllib.TOMLDecodeError as exc:
            msg = f"prompts {path}: expected TOML, got: {exc}"
            raise PromptFileError(msg) from exc

        try:
            source = PromptFile.model_validate(raw)
        except ValidationError as exc:
            msg = (
                f"prompts {path}: expected description, [arguments.<name>] with "
                "description and required, and [[messages]] with role and text: "
                f"{ValidationText.of(exc)}"
            )
            raise PromptFileError(msg) from exc

        arguments: list[PromptArgument] = []
        for name, argument in source.arguments.items():
            arguments.append(
                PromptArgument(
                    name=name,
                    description=argument.description,
                    required=argument.required,
                )
            )

        return FilePrompt(
            name=path.stem,
            description=source.description,
            arguments=arguments,
            source=source,
        )


class FilePrompts(Provider):
    """Провайдер fastmcp: промпты endpoint'а из файлов. Создаётся сервером
    endpoint'а (McpServer) над промптами его каталога."""

    def __init__(self, prompts: Sequence[FilePrompt]) -> None:
        super().__init__()
        self._prompts = list(prompts)

    async def _list_prompts(self) -> Sequence[Prompt]:
        return self._prompts
