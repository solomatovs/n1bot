"""Chainlit-интеграция: sink-мост к UI, сессия, загрузки и конфиг.

Короткие импорты::

    from boba.web.chainlit import ChainlitBridgeSink, ChatSession
    from boba.web.chainlit import ChainlitConfig, ChainlitSection

Модули app и __main__ — точки
входа приложения, их содержимое не реэкспортируется.
"""

from boba.web.chainlit.bridge import ChainlitBridgeSink
from boba.web.chainlit.config import ChainlitConfig, ChainlitSection
from boba.web.chainlit.files import save_upload
from boba.web.chainlit.session import ChatSession
from boba.web.chainlit.ui_overrides import UIOverrideTomlConverter

__all__ = [
    "ChainlitBridgeSink",
    "ChainlitConfig",
    "ChainlitSection",
    "ChatSession",
    "UIOverrideTomlConverter",
    "save_upload",
]
