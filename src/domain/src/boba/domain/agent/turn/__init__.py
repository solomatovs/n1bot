"""Декларативное описание следующего хода агента.

Пять сущностей, разнесённые по назначению:

- :mod:`.effects` — :class:`TurnEffect` + конкретные варианты
  (что именно происходит в диалоге — user-query, tool-result,
  LLM-feedback и далее расширяемо).
- :mod:`.trigger` — :class:`TurnTrigger`: упорядоченный список
  эффектов + теги, накапливается между producer'ами в течение
  итерации.
- :mod:`.spec` — :class:`TurnState` (частично собранный
  :class:`LLMRequest`), :class:`TurnResolveContext` (ctx для
  reducer'ов), :class:`TurnSpec` — фабрика, собирающая
  :class:`LLMRequest` через fold по reducer'ам.
- :mod:`.reducers` — конкретные reducer'ы для осей Model / System /
  History / Tools / Sampling. Используют существующие сервисы
  (:class:`PromptFactory`, :class:`ToolsService`) без дублирования.

Подмодуль НЕ подключен в композицию middleware — это скелет,
который переключится в отдельном коммите.
"""
