"""Chainlit-интеграция: sink-мост к UI, сессия, загрузки и конфиг.

Короткие импорты::

    from boba.chainlit import ChainlitBridgeSink, ChatSession
    from boba.chainlit import chainlit_resolver, load_models

Модули :mod:`boba.chainlit.app` и :mod:`boba.chainlit.__main__` — точки
входа приложения, их содержимое не реэкспортируется.
"""

from boba.chainlit.bridge import ChainlitBridgeSink
from boba.chainlit.config import (
    AUTH_SECRET,
    HEADLESS,
    HOST,
    MODELS,
    PORT,
    ROOT_PATH,
    chainlit_resolver,
    load_models,
)
from boba.chainlit.files import save_upload
from boba.chainlit.session import ChatSession
from boba.chainlit.ui_overrides import (
    UI_ENABLE_TELEMETRY,
    UI_NAME,
    UI_UPLOAD_ACCEPT,
    UI_UPLOAD_MAX_FILES,
    UI_UPLOAD_MAX_MB,
    UIOverride,
    UIOverrideTomlConverter,
    read_ui_override,
)

__all__ = [
    "AUTH_SECRET",
    "HEADLESS",
    "HOST",
    "MODELS",
    "PORT",
    "ROOT_PATH",
    "UI_ENABLE_TELEMETRY",
    "UI_NAME",
    "UI_UPLOAD_ACCEPT",
    "UI_UPLOAD_MAX_FILES",
    "UI_UPLOAD_MAX_MB",
    "ChainlitBridgeSink",
    "ChatSession",
    "UIOverride",
    "UIOverrideTomlConverter",
    "chainlit_resolver",
    "load_models",
    "read_ui_override",
    "save_upload",
]
