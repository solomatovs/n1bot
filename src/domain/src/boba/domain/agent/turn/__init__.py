"""Декларативное описание следующего хода агента.

Две сущности, разнесённые по назначению:

- :mod:`.spec` — :class:`TurnState` (частично собранный
  :class:`LLMRequest`), :class:`TurnResolveContext` (ctx для
  reducer'ов), :class:`TurnSpec` — фабрика, собирающая
  :class:`LLMRequest` через fold по reducer'ам.
- :mod:`.reducers` — конкретные reducer'ы для осей Model / System /
  History / Tools / Sampling. Используют существующие сервисы
  (:class:`PromptFactory`, :class:`ToolsService`,
  :class:`MessageService`) без дублирования.
"""
