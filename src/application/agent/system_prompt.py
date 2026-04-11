"""Генерация system prompt из ToolRegistry."""
from __future__ import annotations

from domain.agent.tools import ToolRegistry

_PREAMBLE = "Ты — эксперт по документации. У тебя есть инструменты для работы с документами."

_STRATEGY = (
    "Стратегия работы:\n"
    "1. Если пользователь ссылается на предыдущий разговор — получи историю чата\n"
    "2. Проиндексируй документы перед первым поиском\n"
    "3. Ищи релевантные фрагменты, можно несколько раз с разными запросами\n"
    "4. Читай файлы для получения полного контекста\n"
    "5. Когда достаточно информации — дай ответ"
)

_RULES = (
    "Правила:\n"
    "- Отвечай ТОЛЬКО на основе найденной информации из документов\n"
    "- Если документы не содержат ответа, скажи об этом прямо\n"
    "- Указывай источники (файл и строки) в ответе"
)


def build_system_prompt(registry: ToolRegistry) -> str:
    """Сгенерировать system prompt из зарегистрированных tools."""
    tools_section = "\n".join(
        f"- {t['function']['name']} — {t['function']['description']}"
        for t in registry.definitions
    )
    return f"{_PREAMBLE}\n\nИнструменты:\n{tools_section}\n\n{_STRATEGY}\n\n{_RULES}"
