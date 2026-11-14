"""Сервис агента boba: ACP, исполнение и история в PostgreSQL."""

from importlib.metadata import version


class Installed:
    def version(self) -> str:
        return version("boba-agent")
