"""boba.db.pgvector — KB-store поверх postgres+pgvector: store-адаптеры, миграции и
bootstrap схемы.

Реэкспорта здесь нет намеренно: store тянет pgvector с numpy, а read-side
инструментам нужен только конфиг. Импорт идёт из подмодуля —
`boba.db.pgvector.config` для моделей, `.store`/`.schema`/`.migrations` для
реализаций.
"""

from __future__ import annotations
