"""Каталог connections и разрешение доступа для MCP-инструментов."""

from boba.identity.sso import RefreshSignal


class NoRefresh(RefreshSignal):
    """Реализация RefreshSignal для сервера без браузерного канала обновления."""

    async def send(self) -> bool:
        return False
