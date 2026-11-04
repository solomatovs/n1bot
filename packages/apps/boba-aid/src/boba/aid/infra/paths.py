"""Каталоги данных, которыми владеет AID."""

from enum import StrEnum


class InstanceData(StrEnum):
    FILES = "files"
    AGENTS = "agents"
