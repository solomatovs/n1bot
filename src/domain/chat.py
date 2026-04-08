"""Чат-домен — сообщения и промпты."""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_SYSTEM_PROMPT = (
    "Ты — эксперт по корпоративной базе знаний. "
    "Отвечай ТОЛЬКО по предоставленному контексту, не ищи ничего в интернете."
)
DEFAULT_USER_TEMPLATE = "Контекст:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."


@dataclass
class PromptParams:
    """Шаблоны промптов, управляемые пользователем."""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_template: str = DEFAULT_USER_TEMPLATE

    def format_user_message(self, context: str, query: str) -> str:
        """Подставить контекст и вопрос в шаблон."""
        return self.user_template.format(context=context, query=query)


@dataclass
class ChatMessage:
    question: str
    answer: str
    thinking: str = ""
    rag_context: str = ""
