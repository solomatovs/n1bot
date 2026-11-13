"""Поля ACP v2 из схемы 3961af94, отсутствующие в SDK 1.0.0rc3."""

from typing import Annotated, Literal

from pydantic import Field

from acp.experimental.v2 import schema


class AvailableCommand(schema.AvailableCommand):
    type: Literal["command"] = "command"


class NewSessionResponse(schema.NewSessionResponse):
    available_commands: Annotated[
        list[AvailableCommand], Field(alias="availableCommands")
    ] = Field(default_factory=list)


class ResumeSessionResponse(schema.ResumeSessionResponse):
    available_commands: Annotated[
        list[AvailableCommand], Field(alias="availableCommands")
    ] = Field(default_factory=list)


class ForkSessionResponse(schema.ForkSessionResponse):
    available_commands: Annotated[
        list[AvailableCommand], Field(alias="availableCommands")
    ] = Field(default_factory=list)
