"""Промпты endpoint'ов сервиса из файлов prompts/<endpoint>/<имя>.toml экземпляра.

Файлы подключает конфиг экземпляра ключом prompts секции endpoint'а
(${toml_dir:prompts/<endpoint>}). Файл — один промпт, его имя — имя файла без
суффикса. Формат:
description — что промпт делает; [arguments.<имя>] — аргумент с
description и required; [[messages]] — сообщения по порядку с role (user
или assistant) и text. В text аргумент подставляется по `{<имя>}`,
отсутствующий необязательный — пустой строкой; фигурная скобка текстом
пишется удвоенной. Промпты публикуются по стандарту MCP: prompts/list и
prompts/get (FilePrompts). Знак ${ в тексте пишется экранированным \\${:
иначе omegaconf примет его за ссылку конфига.

Ошибки:
PromptError — prompts/get не называет обязательный аргумент или называет лишний.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from fastmcp.exceptions import PromptError
from fastmcp.prompts import Message, Prompt, PromptArgument
from fastmcp.server.providers import Provider
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "EndpointPrompts",
    "FilePrompt",
    "FilePrompts",
    "PromptFile",
]


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
    аргументами для prompts/get. Создаёт EndpointPrompts на каждый файл,
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


class EndpointPrompts:
    """Промпты endpoint'а из его секции конфига: объявление fastmcp на каждый
    PromptFile. Файлы промптов подключает сам конфиг экземпляра
    (prompts = "${toml_dir:prompts/<endpoint>}"), сюда приходят уже
    провалидированные модели. Создаёт сборка endpoint'ов (McpEndpoints)."""

    def prompts(self, sources: Mapping[str, PromptFile]) -> Sequence[FilePrompt]:
        prompts: list[FilePrompt] = []
        for name, source in sorted(sources.items()):
            prompts.append(self._prompt(name, source))

        return prompts

    def _prompt(self, name: str, source: PromptFile) -> FilePrompt:
        arguments: list[PromptArgument] = []
        for argument_name, argument in source.arguments.items():
            arguments.append(
                PromptArgument(
                    name=argument_name,
                    description=argument.description,
                    required=argument.required,
                )
            )

        return FilePrompt(
            name=name,
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
