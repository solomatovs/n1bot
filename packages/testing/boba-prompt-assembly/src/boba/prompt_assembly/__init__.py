"""Разбор журнала сессии Claude Code 2.1.289 и сборка запросов к API так, как их строит клиент.

Слои, снизу вверх: records (модели записей и чтение журнала) → loader
(загрузка при --resume, процессы, моменты запросов, список в памяти) →
entries, attachments, cache, messages (сборка messages и меток кэша) →
request (system, tools, параметры) → compaction (сжатие и подрезка) →
session (сборка слоёв и внешние параметры) → oracle (декодер эталона) →
corpus, compaction_checks, verify (сверка) → cli (вход командной строки).
"""
