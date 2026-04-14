"""AppProvider — singleton-сервисы приложения (Scope.APP)."""

from __future__ import annotations

from dishka import Provider, Scope, provide

from boba.domain.config import AppConfig


class AppProvider(Provider):
    """Singleton-сервисы: конфигурация, LLM-клиент, эмбеддинги."""

    scope = Scope.APP

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    @provide
    def config(self) -> AppConfig:
        return self._config
