"""Chainlit-интеграция: sink-мост к UI, сессия, загрузки и конфиг.

Короткие импорты::

    from boba_chainlit import ChainlitBridgeSink, ChatSession
    from boba_chainlit import chainlit_resolver, load_models

Модули :mod:`boba_chainlit.app` и :mod:`boba_chainlit.__main__` — точки
входа приложения, их содержимое не реэкспортируется.
"""

from boba_chainlit.bridge import ChainlitBridgeSink
from boba_chainlit.config import (
    AUTH_SECRET,
    HEADLESS,
    HOST,
    MODELS,
    PORT,
    ROOT_PATH,
    chainlit_resolver,
    load_models,
)
from boba_chainlit.files import save_upload
from boba_chainlit.session import ChatSession
from boba_chainlit.ui_overrides import (
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
