"""Декларативное описание следующего хода агента.

Две сущности, разнесённые по назначению:

- spec — TurnState (частично собранный
  LLMRequest), TurnResolveContext (ctx для
  reducer'ов), TurnSpec — фабрика, собирающая
  LLMRequest через fold по reducer'ам.
- reducers — конкретные reducer'ы для осей Model / System /
  History / Tools / Sampling. Используют существующие сервисы
  (PromptFactory, ToolsService,
  MessageService) без дублирования.
"""
