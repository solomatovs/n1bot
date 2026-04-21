"""Chainlit web UI для boba-agent.

Запуск::

    chainlit run src/chainlit/src/boba/chainlit/app.py

Точка входа — :mod:`boba.chainlit.app`. Мост между синхронным
``AgentHarness`` и async-петлёй Chainlit реализован в
:mod:`boba.chainlit.bridge`.
"""

from boba.chainlit.bridge import ChainlitBridgeSink

__all__ = ["ChainlitBridgeSink"]
