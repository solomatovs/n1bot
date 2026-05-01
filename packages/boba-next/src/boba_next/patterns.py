"""Re-export shim: содержимое живёт в `boba_patterns` (пакет `boba-patterns`).

Оставлено для обратной совместимости; новые callsite'ы импортируют напрямую
из `boba_patterns`.
"""

from boba_patterns import *  # noqa: F401, F403
