"""Разрешения на вызов инструмента: режимы, правила сессии, решение.

Шесть режимов — план, раздел 5.6; режим и выданные правила живут только в
истории (раздел 5.5): PermissionState сворачивает записи `permission-mode`
(последняя побеждает) и `permission-rule` (правило действует, пока
последняя операция над ним — `add`). PermissionPolicy решает по режиму,
пометкам инструмента и правилам: разрешить, отказать с текстом, спросить
наружу или спросить классификатор. Классификатор режима `auto` —
AutoClassifier над `SchemaReply`: сбой или неразборчивый ответ — отказ,
не разрешение.

Ошибки:
PermissionModeError — режим нельзя включить: `bypassPermissions` без
    разрешения в параметрах запуска, `auto` без классификатора.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from boba.agent.records import (
    PermissionBehavior,
    PermissionMode,
    PermissionModeRecord,
    PermissionRule,
    PermissionRuleOperation,
    PermissionRuleRecord,
    ServiceRecord,
)
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.llm.chat import ChatModel, LlmError, ToolSpec
from boba.llm.schema import SchemaReply
from boba.toolkit.dag import ToolHints

logger = logging.getLogger(__name__)

__all__ = [
    "Allowed",
    "Asked",
    "AutoClassifier",
    "Classified",
    "Decision",
    "Denied",
    "ModeCeiling",
    "PermissionModeError",
    "PermissionPolicy",
    "PermissionState",
    "StaticRules",
]


class PermissionModeError(Exception):
    """Режим разрешений нельзя включить при этих параметрах запуска."""


@dataclass(frozen=True)
class Allowed:
    """Вызов идёт без вопроса."""


@dataclass(frozen=True)
class Denied:
    """Вызов не идёт; text — результат-ошибка для модели."""

    text: str


@dataclass(frozen=True)
class Asked:
    """Решение за запускающей программой: вопрос `can_use_tool`."""


@dataclass(frozen=True)
class Classified:
    """Решение за классификатором режима `auto`."""


Decision = Allowed | Denied | Asked | Classified


class PermissionState:
    """Свёртка служебных записей сессии: действующий режим и правила."""

    def __init__(self, records: Sequence[ServiceRecord]) -> None:
        self._mode = PermissionMode.DEFAULT
        self._allowed: dict[str, PermissionRule] = {}
        for record in records:
            self._fold(record)

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def rules(self) -> Sequence[PermissionRule]:
        return list(self._allowed.values())

    def allows(self, tool: str) -> bool:
        return tool in self._allowed

    def _fold(self, record: ServiceRecord) -> None:
        if isinstance(record, PermissionModeRecord):
            self._mode = record.permission_mode
            return

        if not isinstance(record, PermissionRuleRecord):
            return

        rule = record.rule
        if rule.behavior is not PermissionBehavior.ALLOW:
            return

        if record.operation is PermissionRuleOperation.ADD:
            self._allowed[rule.tool] = rule
            return

        self._allowed.pop(rule.tool, None)


class ModeCeiling:
    """Потолок режима из параметров запуска: журнал задаёт значение не выше.

    `bypassPermissions` без `allow_bypass` понижается до `default` с
    предупреждением; `auto` без классификатора — ошибка, понижать его
    молча нельзя: пользователь ждал автоматических ответов.
    """

    def __init__(self, allow_bypass: bool, has_classifier: bool) -> None:
        self._allow_bypass = allow_bypass
        self._has_classifier = has_classifier

    def check(self, mode: PermissionMode) -> None:
        """Режим, запрошенный командой: нарушение потолка — ошибка.

        Ошибки:
        PermissionModeError — режим недоступен при этих параметрах запуска.
        """
        if mode is PermissionMode.BYPASS_PERMISSIONS and not self._allow_bypass:
            msg = (
                "The permission mode 'bypassPermissions' is not allowed: the agent was "
                "started without allow_bypass. Start it with allow_bypass to disable "
                "permission checks, or pick another mode."
            )
            raise PermissionModeError(msg)

        if mode is PermissionMode.AUTO and not self._has_classifier:
            msg = (
                "The permission mode 'auto' needs a classifier model and its "
                "instruction, but the agent was started without them. Configure the "
                "classifier, or pick another mode."
            )
            raise PermissionModeError(msg)

    def capped(self, mode: PermissionMode) -> PermissionMode:
        """Режим из журнала или начальный: `bypassPermissions` без права на
        него становится `default`; `auto` без классификатора — ошибка.

        Ошибки:
        PermissionModeError — `auto` без классификатора.
        """
        if mode is PermissionMode.BYPASS_PERMISSIONS and not self._allow_bypass:
            logger.warning(
                "permissions: the mode 'bypassPermissions' is lowered to 'default': "
                "the agent runs without allow_bypass"
            )
            return PermissionMode.DEFAULT

        self.check(mode)

        return mode


@dataclass(frozen=True)
class StaticRules:
    """Правила из настроек запуска: действуют всю сессию и в журнал не
    пишутся. Запрет побеждает всё, включая режим `bypassPermissions`."""

    allowed: frozenset[str] = frozenset()
    denied: frozenset[str] = frozenset()


class PermissionPolicy:
    """Решение по вызову из режима, пометок инструмента, правил настроек и
    правил сессии."""

    def __init__(
        self,
        state: PermissionState,
        templates: AgentTemplate,
        agent_name: str,
        static: StaticRules,
    ) -> None:
        self._state = state
        self._templates = templates
        self._agent = agent_name
        self._static = static

    @property
    def mode(self) -> PermissionMode:
        return self._state.mode

    def decide(self, tool: str, hints: ToolHints) -> Decision:
        if tool in self._static.denied:
            text = self._templates.read(AgentTemplateFile.DENIED_BY_SETTINGS)
            return Denied(text.format(tool=tool))

        mode = self._state.mode
        if mode is PermissionMode.BYPASS_PERMISSIONS:
            return Allowed()

        if hints.read_only:
            return Allowed()

        if mode is PermissionMode.PLAN:
            text = self._templates.read(AgentTemplateFile.PLAN_MODE)
            return Denied(text.format(tool=tool))

        if self._granted(tool):
            return Allowed()

        return self._unruled(tool, hints, mode)

    def _granted(self, tool: str) -> bool:
        """Разрешён правилом настроек или правилом сессии."""
        if tool in self._static.allowed:
            return True

        return self._state.allows(tool)

    def _unruled(self, tool: str, hints: ToolHints, mode: PermissionMode) -> Decision:
        """Меняющий вызов без правила: решение зависит только от режима."""
        if mode is PermissionMode.ACCEPT_EDITS and not hints.destructive:
            return Allowed()

        if mode is PermissionMode.DONT_ASK:
            text = self._templates.read(AgentTemplateFile.NOT_GRANTED)
            return Denied(text.format(agent=self._agent, tool=tool))

        if mode is PermissionMode.AUTO:
            return Classified()

        return Asked()


class ClassifierVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ClassifierReply(BaseModel):
    """Форма ответа классификатора."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    decision: ClassifierVerdict
    reason: str = ""


class AutoClassifier:
    """Классификатор режима `auto`: отдельное обращение к модели по схеме.

    Создаёт агент из параметров запуска: модель классификатора и инструкция
    — системный промпт обращения. На вход — имя инструмента, его пометки и
    аргументы вызова; на выход — решение с причиной. Любой сбой: ошибка
    модели, ответ не по схеме — отказ с текстом причины.
    """

    SCHEMA_NAME: ClassVar[str] = "permission_decision"

    def __init__(
        self, model: ChatModel, instruction: str, templates: AgentTemplate
    ) -> None:
        self._reply = SchemaReply(model, {})
        self._instruction = instruction
        self._templates = templates
        self._schema = ToolSpec(
            name=self.SCHEMA_NAME,
            description="Decide whether the tool call may run.",
            parameters=ClassifierReply.model_json_schema(),
        )

    async def decide(
        self, tool: str, hints: ToolHints, arguments: Mapping[str, Any]
    ) -> Allowed | Denied:
        question = self._question(tool, hints, arguments)
        try:
            raw = await self._reply.ask(self._instruction, question, self._schema)
            verdict = ClassifierReply.model_validate(raw)
        except (LlmError, ValidationError, ValueError) as exc:
            logger.warning(
                "permissions: the auto classifier failed on %s: %s", tool, exc
            )
            text = self._templates.read(AgentTemplateFile.CLASSIFIER_FAILED)
            return Denied(text.format(tool=tool, error=str(exc)))

        if verdict.decision is ClassifierVerdict.ALLOW:
            return Allowed()

        text = self._templates.read(AgentTemplateFile.CLASSIFIER_DENIED)

        return Denied(text.format(tool=tool, reason=verdict.reason))

    def _question(
        self, tool: str, hints: ToolHints, arguments: Mapping[str, Any]
    ) -> str:
        return (
            f"Tool: {tool}\n"
            f"Read-only: {hints.read_only}\n"
            f"Destructive: {hints.destructive}\n"
            f"Arguments: {dict(arguments)}"
        )
