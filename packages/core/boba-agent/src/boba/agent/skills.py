"""Скиллы агента: чтение каталога, карточки для списка, инструмент `Skill`.

Скилл — каталог с файлом SKILL.md: заголовок между строками `---` даёт имя
и описание, остальное — текст инструкций. Читатель SkillsDir — граница
конфига: он один ходит на диск, дальше скиллы живут моделями Skill.
Инструмент `Skill` — собственный инструмент агента (план, раздел 5.12):
результатом отдаёт текст скилла.

Ошибки:
SkillsError — каталог скиллов не читается или заголовок SKILL.md не разобран.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.attachments import SnapshotSkill, SnapshotTool
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.agent.tools import CallResult, OwnTool, PlannedCall, Questioner
from boba.toolkit.dag import ToolHints

__all__ = ["Skill", "SkillTool", "SkillsDir", "SkillsError"]


class SkillsError(Exception):
    """Каталог скиллов не читается или файл скилла не разобран."""


class SkillFile(StrEnum):
    """Устройство каталога скилла."""

    MANIFEST = "SKILL.md"
    FENCE = "---"
    NAME = "name"
    DESCRIPTION = "description"
    ENCODING = "utf-8"


class Skill(BaseModel):
    """Скилл: имя, описание для списка, текст инструкций."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str
    content: str

    def card(self) -> SnapshotSkill:
        return SnapshotSkill(name=self.name, description=self.description)


class SkillsDir:
    """Читатель каталога скиллов: по подкаталогу на скилл, в каждом SKILL.md.

    Создаёт вход процесса из параметра запуска `--skills-dir`; тесты — из
    временного каталога. Подкаталог без SKILL.md пропускается.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def read(self) -> Sequence[Skill]:
        """Ошибки:
        SkillsError — каталога нет или заголовок SKILL.md не разобран.
        """
        if not self._root.is_dir():
            msg = (
                f"The skills directory {self._root} does not exist or is not a "
                "directory. Point --skills-dir at a directory with one subdirectory "
                "per skill, each holding a SKILL.md."
            )
            raise SkillsError(msg)

        return list(self._skills())

    def _skills(self) -> Iterator[Skill]:
        for child in sorted(self._root.iterdir()):
            manifest = child / SkillFile.MANIFEST.value
            if not manifest.is_file():
                continue

            yield self._parsed(child.name, manifest)

    def _parsed(self, folder: str, manifest: Path) -> Skill:
        try:
            text = manifest.read_text(encoding=SkillFile.ENCODING.value)
        except OSError as exc:
            msg = f"Cannot read the skill file {manifest}: {exc}"
            raise SkillsError(msg) from exc

        header, body = self._split(manifest, text)
        name = header.get(SkillFile.NAME.value, folder)
        if not name:
            name = folder

        return Skill(
            name=name,
            description=header.get(SkillFile.DESCRIPTION.value, ""),
            content=body.strip(),
        )

    def _split(self, manifest: Path, text: str) -> tuple[Mapping[str, str], str]:
        """Заголовок `key: value` между строками `---` и тело после него;
        без заголовка весь файл — тело."""
        lines = text.splitlines()
        if not lines or lines[0].strip() != SkillFile.FENCE.value:
            return {}, text

        header: dict[str, str] = {}
        for index in range(1, len(lines)):
            line = lines[index]
            if line.strip() == SkillFile.FENCE.value:
                return header, "\n".join(lines[index + 1 :])

            key, separator, value = line.partition(":")
            if not separator:
                msg = (
                    f"The skill file {manifest} has a header line without 'key: value' "
                    f"shape: {line!r}. Fix the front matter between the '---' lines."
                )
                raise SkillsError(msg)

            header[key.strip()] = value.strip().strip('"')

        msg = (
            f"The skill file {manifest} opens a '---' header that never closes. "
            "Add the closing '---' line after the front matter."
        )
        raise SkillsError(msg)


class SkillCallInput(BaseModel):
    """Ввод инструмента `Skill`."""

    model_config = ConfigDict(frozen=True)

    skill: str = Field(description="exact name from the listing, no leading slash")
    args: str = Field(default="", description="optional arguments to pass through")


class SkillTool(OwnTool):
    """Инструмент `Skill`: по имени отдаёт текст скилла результатом.

    Создаёт агент, когда скиллы заданы. Текст уходит как есть; аргументы
    вызова дописываются отдельной строкой, чтобы модель видела, с чем скилл
    вызван.
    """

    NAME: ClassVar[str] = "Skill"
    HINTS: ClassVar[ToolHints] = ToolHints(read_only=True, destructive=False)
    ARGS_LINE: ClassVar[str] = "ARGUMENTS: {args}"

    def __init__(self, skills: Sequence[Skill], templates: AgentTemplate) -> None:
        self._templates = templates
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self._skills[skill.name] = skill

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def hints(self) -> ToolHints:
        return self.HINTS

    def card(self) -> SnapshotTool:
        return SnapshotTool(
            name=self.NAME,
            description=self._templates.read(AgentTemplateFile.SKILL),
            schema=SkillCallInput.model_json_schema(),
        )

    async def run(
        self, call: PlannedCall, arguments: Mapping[str, Any], questioner: Questioner
    ) -> CallResult:
        requested = str(arguments.get("skill", ""))
        skill = self._skills.get(requested)
        if skill is None:
            text = self._templates.read(AgentTemplateFile.NO_SUCH_SKILL).format(
                skill=requested
            )
            return CallResult(call=call, text=text, is_error=True, artifact=text)

        parts = [
            self._templates.read(AgentTemplateFile.SKILL_LAUNCHED).format(
                skill=skill.name
            ),
            skill.content,
        ]
        args = str(arguments.get("args", ""))
        if args:
            parts.append(self.ARGS_LINE.format(args=args))

        text = "\n\n".join(parts)
        artifact = {"skill": skill.name, "args": args}

        return CallResult(call=call, text=text, is_error=False, artifact=artifact)
