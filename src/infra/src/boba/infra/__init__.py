"""Публичный API infra-слоя: DI-контейнер и его наполнение.

Импорты короткой формой::

    from boba.infra import create_container, request_scope
    from boba.infra import AppProvider, RequestProvider, ConfigLoader
    from boba.infra import AgentHarness, CapturingSink
"""

from boba.infra.app import AppProvider, RequestProvider
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope
from boba.infra.harness import AgentHarness, CapturingSink

__all__ = [
    "AgentHarness",
    "AppProvider",
    "CapturingSink",
    "ConfigLoader",
    "RequestProvider",
    "create_container",
    "request_scope",
]
