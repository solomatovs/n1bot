"""Middleware-обёртки LLM-слоя.

Каждый модуль (``retry.py``, ...) держит middleware-класс своей
зоны ответственности; здесь — агрегирующий реэкспорт. Публичный API
LLM-слоя — :mod:`boba.domain.llm`; внешний код должен импортировать
оттуда.

Симметрично :mod:`boba.domain.agent.middleware`: separation между
middleware-обёртками и базовыми контрактами/DTO. Если LLM-слою
понадобится новая middleware-стадия — она ложится сюда без выноса
в плоский namespace.
"""

from boba.domain.llm.middleware.retry import RetryMiddleware

__all__ = ["RetryMiddleware"]
