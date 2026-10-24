"""Вид разговора: вопрос пользователя, ответ модели и ход — контейнер, кружок
ожидания, рассуждения — в обеих темах и обоих размерах окна.

Вопросы — обычный текст, ответы — книга ответов фейковой модели
(TalkBook): по точному тексту вопроса модель отвечает осмысленно, зовёт
инструмент с человеческой подписью или держит поток на паузе, пока тест
снимает кадр «идёт». Каждое сочетание темы и размера — свой вход и свой
тред; книга кладётся заново, чтобы паузы снова держали поток. Кадр ленты —
видимая область прокрутки сообщений вместе с кнопкой «↓», прокрученная к
вопросу хода. Ход, открытый заново после перезагрузки, сверяется с живым
кадром пиксель в пиксель: лента из истории обязана совпасть с живой.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import pytest
from aid_look import (
    LookCheck,
    LookError,
    LookFix,
    LookPages,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from PIL import Image, ImageDraw
from playwright.sync_api import (
    Error,
    Locator,
    Page,
    ViewportSize,
    WebSocket,
    WebSocketRoute,
    expect,
)

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import Reply, ReplyBookClient, ReplyCall, ReplyTurn
from boba.stand.ui.socket_log import SocketLog
from boba.stand.ui.stand import StandAuth, StandProcess, StandUrl

pytestmark = [pytest.mark.ui, pytest.mark.look]


class TalkQuestion(StrEnum):
    """Вопросы разговора: ключи книги ответов и текст в пузыре вопроса."""

    STATUS = "Какой сегодня статус выгрузки?"
    COMPARE = (
        "Сравни два подхода к выгрузке:\n1) ночной пакет;\n2) поток изменений.\n"
        "Какой дешевле в поддержке?\n\nКонтекст: витрина продаж обновляется раз в "
        "сутки, к утру отчёты должны быть готовы, а аналитики жалуются, что днём "
        "данные устаревают. Ночной пакет перегружает всю таблицу за два часа, поток "
        "изменений требует отдельного сервиса и следит за журналом базы. Команда "
        "небольшая, дежурств ночью нет, а любые сбои должны чиниться утром без "
        "ручной перезаливки. Нужна оценка трудозатрат на поддержку за год и риски "
        "каждого варианта, если объём данных вырастет вдвое."
    )
    WHY = "Почему падает `SELECT *`?\n- в проде\n- на стенде"
    FILES = "Посмотри эти файлы"
    DAY = "Сколько заказов за вчера?"
    WEEK = "Сколько заказов за неделю?"
    REPORT = "Покажи пример отчёта по продажам за квартал"
    MARGIN = "Как считается маржа?"
    HISTORY = "Расскажи подробно историю проекта"
    STEPS = "Опиши шаги миграции"
    PLAN = "Составь полный план миграции"
    STORAGE = "Проверь доступность хранилища"
    CONNECTIONS = "Какие подключения мне доступны?"
    PARTITIONS = "Подумай, как разбить таблицу на партиции"
    SOURCES = "Какие источники данных подключены?"


class TalkText(StrEnum):
    """Тексты ответов книги: по смыслу вопроса."""

    STATUS = "Выгрузка закончилась в 06:10, ошибок нет."
    COMPARE = (
        "Дешевле в поддержке ночной пакет: один процесс, понятный перезапуск и "
        "никаких дежурств. Поток изменений окупится, только если данные нужны "
        "в течение дня."
    )
    WHY = (
        "В проде у таблицы есть колонка с типом, которого нет на стенде: "
        "`SELECT *` тянет её и падает на приведении. Перечислите колонки явно."
    )
    FILES = "Файлы получены: картинка, отчёт на одну страницу и таблица."
    DAY = "Вчера оформили 1 284 заказа."
    WEEK = "За неделю оформили 8 917 заказов."
    REPORT = "\n".join(
        (
            "# Отчёт по продажам за III квартал",
            "## Итоги",
            "Выручка выросла на **12 %**, средний чек — на *3 %*. Данные — из "
            "витрины `sales_q3`.",
            "### Регионы",
            "- Север",
            "  - Мурманск",
            "  - Архангельск",
            "- Юг",
            "",
            "1. Собрать данные",
            "2. Проверить дубли",
            "3. Отправить отчёт",
            "",
            "| Регион | Июль | Август | Сентябрь |",
            "|:--|--:|--:|:-:|",
            "| Север | 1 200 | 1 340 | 1 410 |",
            "| Юг | 2 010 | 1 980 | 2 150 |",
            "| Запад | 880 | 920 | 1 005 |",
            "| Итого | 4 090 | 4 240 | 4 565 |",
            "",
            "```python",
            "total = sum(row.amount for row in rows)",
            "```",
            "",
            "```sql",
            "SELECT region, sum(amount) FROM sales GROUP BY region;",
            "```",
            "",
            "```json",
            '{"region": "Север", "amount": 3950}',
            "```",
            "",
            "```bash",
            "psql -c \"\\copy sales to 'q3.csv' csv header\"",
            "```",
            "",
            "> Данные за сентябрь предварительные.",
            "",
            "Подробнее — [методика](https://example.com/method) и [итоги](#итоги).",
            "",
            "---",
        )
    )
    MARGIN = "\n".join(
        (
            "Маржа — доля цены, которая остаётся после себестоимости: "
            "$m = (p - c) / p$, где $p$ — цена, $c$ — себестоимость.",
            "",
            "Суммарная маржа портфеля:",
            "",
            "$$",
            "\\sum_i m_i",
            "$$",
            "",
            "При ценах $5 и $10 и себестоимости $4 маржа — 20 % и 60 %.",
            "",
            "Строка <b>не жирный</b> остаётся текстом.",
        )
    )
    STORAGE = "Хранилище доступно, свободно 120 ГБ."
    LOOKING = "Смотрю подключения."
    CONNECTIONS = "Подключения выше: пока ни одного, их можно добавить в настройках."
    PARTITIONS = (
        "Разбейте таблицу по месяцам даты заказа: запросы отчётов читают один "
        "месяц, а старые партиции можно отсоединять целиком."
    )
    CHECKING = "Проверяю источники."
    SOURCES = "Источников пока нет: подключения добавляются в настройках."


class TalkBook:
    """Книга ответов разговора: вопрос → ходы модели. Паузы потока: поток
    ответа о шагах миграции стоит после двенадцати слов, плана — после
    двадцати (его останавливает пользователь), проверки хранилища — до
    первого токена, рассуждения о партициях — после пятнадцати слов, а
    ответ после вызова подключений — до первого токена, пока тест снимает
    кружок под шагом вызова; так же ждёт ответ после вызова с длинной
    подписью, пока тест снимает строку идущего хода."""

    STEPS_TOKENS: ClassVar[int] = 12
    PLAN_TOKENS: ClassVar[int] = 20
    THINKING_TOKENS: ClassVar[int] = 15
    TOOL: ClassVar[str] = "connection_list"
    INTENT: ClassVar[str] = "Смотрю подключения"
    LONG_INTENT: ClassVar[str] = (
        "Перебираю все подключения пользователя по профилям и серверам, чтобы "
        "найти базы, хранилища и сервисы с их адресами и сроками билетов"
    )
    STEPS_SEED: ClassVar[str] = (
        "Сначала заморозьте схему, затем перенесите справочники, после них — "
        "факты по месяцам, сверьте суммы и переключите отчёты."
    )
    PLAN_SEED: ClassVar[str] = (
        "Подготовка стенда, перенос справочников, перенос фактов, сверка, "
        "переключение отчётов, наблюдение неделю и отключение старой витрины."
    )
    THINKING_SEED: ClassVar[str] = (
        "Таблица растёт на миллион строк в месяц, отчёты читают месяц или "
        "квартал, значит ключ — дата заказа, а интервал — месяц."
    )

    def replies(self) -> list[Reply]:
        connections = ReplyCall(tool=self.TOOL, arguments={}, intent=self.INTENT)
        sources = ReplyCall(tool=self.TOOL, arguments={}, intent=self.LONG_INTENT)
        steps = self.words(80, self.STEPS_SEED)
        plan = self.words(400, self.PLAN_SEED)
        return [
            self._plain(TalkQuestion.STATUS, TalkText.STATUS),
            self._plain(TalkQuestion.COMPARE, TalkText.COMPARE),
            self._plain(TalkQuestion.WHY, TalkText.WHY),
            self._plain(TalkQuestion.FILES, TalkText.FILES),
            self._plain(TalkQuestion.DAY, TalkText.DAY),
            self._plain(TalkQuestion.WEEK, TalkText.WEEK),
            self._plain(TalkQuestion.REPORT, TalkText.REPORT),
            self._plain(TalkQuestion.MARGIN, TalkText.MARGIN),
            self._plain(TalkQuestion.HISTORY, self.history()),
            Reply(
                question=TalkQuestion.STEPS,
                turns=[
                    ReplyTurn(
                        content=steps,
                        hold=True,
                        hold_after_tokens=self.STEPS_TOKENS,
                    )
                ],
            ),
            Reply(
                question=TalkQuestion.PLAN,
                turns=[
                    ReplyTurn(
                        content=plan,
                        hold=True,
                        hold_after_tokens=self.PLAN_TOKENS,
                    )
                ],
            ),
            Reply(
                question=TalkQuestion.STORAGE,
                turns=[ReplyTurn(content=TalkText.STORAGE, hold=True)],
            ),
            Reply(
                question=TalkQuestion.CONNECTIONS,
                turns=[
                    ReplyTurn(content=TalkText.LOOKING, calls=[connections]),
                    ReplyTurn(content=TalkText.CONNECTIONS, hold=True),
                ],
            ),
            Reply(
                question=TalkQuestion.SOURCES,
                turns=[
                    ReplyTurn(content=TalkText.CHECKING, calls=[sources]),
                    ReplyTurn(content=TalkText.SOURCES, hold=True),
                ],
            ),
            Reply(
                question=TalkQuestion.PARTITIONS,
                turns=[
                    ReplyTurn(
                        reasoning=self.thinking(),
                        content=TalkText.PARTITIONS,
                        hold=True,
                        hold_after_tokens=self.THINKING_TOKENS,
                    )
                ],
            ),
        ]

    def thinking(self) -> str:
        return self.words(60, self.THINKING_SEED)

    def words(self, count: int, seed: str) -> str:
        """Связный текст из count слов: повторяется фраза seed."""
        source = seed.split(" ")
        picked: list[str] = []
        for index in range(count):
            picked.append(source[index % len(source)])

        return " ".join(picked)

    def history(self) -> str:
        """Сорок абзацев истории проекта — ответ на три экрана."""
        paragraphs: list[str] = []
        for number in range(1, 41):
            paragraphs.append(
                f"**Этап {number}.** Перенесено отчётов на новую витрину: "
                f"{number * 3}, закрыто старых выгрузок: {number}. Пользователи "
                "получили данные раньше, а ночные сбои стали реже."
            )

        return "\n\n".join(paragraphs)

    def streamed(self, text: str, count: int) -> str:
        """Начало text, которое поток отдал до паузы после count слов."""
        return " ".join(text.split(" ")[:count])

    def _plain(self, question: TalkQuestion, answer: str) -> Reply:
        return Reply(question=question, turns=[ReplyTurn(content=answer)])


class TalkFixes:
    """Правки вида, которые показывают кадры разговора: что было и что стало."""

    PLAIN: ClassVar[LookFix] = LookFix(
        code="Вопросы и ответы обычным текстом",
        summary=(
            "было: вопрос в пузыре — служебная строка теста «scenario …» с JSON, "
            "подпись вызова «stand call of …», ответы-заготовки «ok» и «the tool "
            "has answered»; стало: вопрос — обычная фраза, ответ — по смыслу "
            "вопроса, у вызова человеческая подпись"
        ),
    )
    CONTAINER: ClassVar[LookFix] = LookFix(
        code="Строка хода только у хода с шагами",
        summary=(
            "было: вживую каждый ход сразу открывал строку «Запрос отправлен…», "
            "у хода из одного ответа она оставалась пустой строкой «Завершено», а "
            "после перезагрузки её не было; стало: пока модель молчит, под "
            "вопросом мигает только кружок, строка хода появляется с первым "
            "шагом (размышления, вызов) — одинаково вживую и после перезагрузки"
        ),
    )
    LOADING: ClassVar[LookFix] = LookFix(
        code="Загрузка истории — не ход",
        summary=(
            "было: пока тред открывался, лента была пустой, а у поля ввода "
            "стояла кнопка «Стоп», как будто идёт ход; стало: под уже "
            "пришедшими сообщениями крутится значок «Загружаем беседу…», "
            "отправка неактивна, «Стоп» нет"
        ),
    )
    RELOAD: ClassVar[LookFix] = LookFix(
        code="После перезагрузки — то же, что вживую",
        summary=(
            "было: после перезагрузки страницы пропадали пометка «остановлено "
            "пользователем» у ответа и сообщение об ошибке хода; стало: обе "
            "пометки — часть истории, кадр после перезагрузки совпадает с живым "
            "пиксель в пиксель (тест сверяет это сам); ошибку хода после "
            "перезагрузки покажут кадры группы ошибок"
        ),
    )
    ATTACHMENTS: ClassVar[LookFix] = LookFix(
        code="Вложения вопроса",
        summary=(
            "было: все вложения — одинаковые плашки-ссылки, клик скачивал файл, "
            "картинки не видно; стало: картинка — превью над вопросом шириной "
            "до 300 px, как картинки chainlit (клик открывает её крупно), файл, "
            "который умеет показать панель справа (PDF, текст, журнал), — "
            "кнопкой «открыть в панели», как файлы в ответах, остальное "
            "(таблица CSV) — плашкой chainlit для скачивания"
        ),
    )
    SCROLL: ClassVar[LookFix] = LookFix(
        code="Кнопка «↓» только когда ниже есть что читать",
        summary=(
            "было: круглая кнопка «↓» висела над лентой, хотя ниже было только "
            "пустое место под последним вопросом, и закрывала содержимое; "
            "стало: кнопка видна, только если ниже ленты есть непрочитанное"
        ),
    )
    SPACER: ClassVar[LookFix] = LookFix(
        code="Пустое место под последним вопросом",
        summary=(
            "было: место под последним вопросом считалось до того, как "
            "догрузятся картинка вложения или карточка приложения, и потом не "
            "менялось: лента то упиралась в конец раньше вопроса, то оставляла "
            "лишнее пустое место, а кадр с тем же содержимым выходил то на 8, "
            "то на 24 px выше; стало: место пересчитывается, когда вопрос или "
            "лента под ним меняют высоту, положение ленты одно и то же"
        ),
    )
    DONE: ClassVar[LookFix] = LookFix(
        code="Подпись законченного хода",
        summary=(
            "было: строка хода и после конца подписана «process...» — "
            "троеточие говорило «ещё идёт»; стало: идущий ход — «Работаю…», "
            "законченный — «Завершено · шагов: N»"
        ),
    )
    CURRENT: ClassVar[LookFix] = LookFix(
        code="Строка идущего хода — его текущее действие",
        summary=(
            "было: пока ход идёт, свёрнутая строка хода неизменно подписана "
            "«Работаю…» — что происходит, видно, только если её раскрыть; "
            "стало: строка подписана текущим действием хода так же, как его "
            "шаг (размышления, вызов с подписью и статусом, вопрос), и мерцает, "
            "как идущий вызов; длинная подпись обрезается многоточием по ширине "
            "строки; по концу хода — «Завершено · шагов: N» без мерцания"
        ),
    )
    THOUGHT: ClassVar[LookFix] = LookFix(
        code="Отметка законченных размышлений",
        summary=(
            "было: у законченных размышлений оставался кружок «идёт»; стало: "
            "галочка «✓ Размышления»"
        ),
    )


class TalkLook:
    """Состояния разговора (QU, AN, TR плана тестов вида)."""

    GROUP: ClassVar[str] = "conversation"
    SHORT: ClassVar[LookState] = LookState(
        GROUP,
        "qu1-short",
        "Короткий вопрос и короткий ответ; ход закончен",
        "вопрос — обычная фраза в пузыре справа; ответ по смыслу вопроса с "
        "аватаром ассистента; строки хода нет (у хода нет шагов); кружка "
        "ожидания нет; кнопки «↓» нет — ниже ленты ничего",
        (TalkFixes.PLAIN, TalkFixes.CONTAINER, TalkFixes.SCROLL),
    )
    LONG: ClassVar[LookState] = LookState(
        GROUP,
        "qu2-long",
        "Многострочный вопрос с длинным абзацем",
        "вопрос размечен как markdown: «1)» и «2)» — нумерованный список, "
        "строка после списка дописана ко второму пункту, пустая строка "
        "отделяет абзац; пузырь не шире 70 % ленты, длинный абзац переносится "
        "по словам; прокрутки вбок нет; на узком окне ниже есть текст — видна "
        "кнопка «↓»",
        (TalkFixes.PLAIN,),
    )
    MARKDOWN: ClassVar[LookState] = LookState(
        GROUP,
        "qu3-markdown",
        "Вопрос с разметкой: код и список",
        "`SELECT *` — моноширинным, «в проде» и «на стенде» — пунктами списка",
        (TalkFixes.PLAIN,),
    )
    COMPOSER: ClassVar[LookState] = LookState(
        GROUP,
        "qu4-composer",
        "Поле ввода с тремя вложениями до отправки",
        "над полем три плашки chainlit: картинка, PDF и таблица, у каждой "
        "крестик удаления; текст вопроса в поле",
        (),
    )
    ATTACHED: ClassVar[LookState] = LookState(
        GROUP,
        "qu4-attached",
        "Отправленный вопрос с вложениями",
        "над пузырём вопроса сверху вниз: кнопка «report.pdf» со значком "
        "панели (откроет PDF справа), превью картинки шириной до 300 px (клик "
        "открывает её крупно), плашка «table.csv» с иконкой — скачивание; "
        "ответ ниже",
        (TalkFixes.ATTACHMENTS, TalkFixes.SPACER),
    )
    EDITING: ClassVar[LookState] = LookState(
        GROUP,
        "qu6-editing",
        "Правка вопроса: поле правки на месте пузыря",
        "поле правки во всю ширину ленты с новым текстом, кнопки «Отмена» и "
        "«Подтвердить» по-русски; ответ на прежний вопрос ещё виден",
        (),
    )
    EDITED: ClassVar[LookState] = LookState(
        GROUP,
        "qu7-edited",
        "После правки: прежний ответ срезан, новый ход",
        "в пузыре новый вопрос «за неделю», под ним только ответ на него; "
        "прежнего ответа нет",
        (TalkFixes.PLAIN,),
    )
    DOCUMENT: ClassVar[LookState] = LookState(
        GROUP,
        "an2-document",
        "Ответ-документ целиком: заголовки, списки, таблица, код, цитата, ссылки",
        "три уровня заголовков; жирный, курсив и `sales_q3` моноширинным; "
        "вложенный список с отступом; нумерованный список; таблица с "
        "выравниванием (регион влево, числа вправо, сентябрь по центру); блоки "
        "кода python, sql, json, bash с подсветкой и кнопкой копирования; "
        "цитата с чертой слева; две ссылки; линия в конце",
        (TalkFixes.PLAIN,),
    )
    TABLE: ClassVar[LookState] = LookState(
        GROUP,
        "an4-table",
        "Таблица ответа крупно",
        "шапка выделена, границы ячеек видны в обеих темах, числа выровнены "
        "вправо, на узком окне таблица прокручивается внутри себя, а не страница",
        (),
    )
    CODE: ClassVar[LookState] = LookState(
        GROUP,
        "an5-code",
        "Блок кода sql крупно",
        "шапка блока с языком «sql» и кнопкой копирования, подсветка ключевых "
        "слов, тёмный фон в обеих темах",
        (),
    )
    FORMULAS: ClassVar[LookState] = LookState(
        GROUP,
        "an7-formulas",
        "Формулы, деньги и HTML в ответе",
        "$m = (p - c) / p$ и сумма — формулами KaTeX (сумма — отдельной "
        "строкой по центру); «$5 и $10 … $4» — обычным текстом, не формулой; "
        "<b>не жирный</b> — текстом с угловыми скобками, не жирным",
        (TalkFixes.PLAIN,),
    )
    HISTORY_TOP: ClassVar[LookState] = LookState(
        GROUP,
        "an9-long-top",
        "Длинный ответ на три экрана: начало, ниже есть непрочитанное",
        "вопрос вверху, начало ответа; внизу по центру круглая кнопка «↓» — "
        "ниже ещё много текста",
        (TalkFixes.SCROLL,),
    )
    HISTORY_END: ClassVar[LookState] = LookState(
        GROUP,
        "an9-long-end",
        "Длинный ответ прокручен до конца",
        "последние этапы ответа, лента прокручена до конца; кнопки «↓» нет",
        (TalkFixes.SCROLL,),
    )
    STREAMING: ClassVar[LookState] = LookState(
        GROUP,
        "an10-streaming",
        "Ответ идёт: первые двенадцать слов и мигающий курсор",
        "двенадцать слов ответа и курсор в конце строки; строки хода нет, "
        "лишнего кружка под ответом нет",
        (TalkFixes.CONTAINER,),
    )
    STOPPED: ClassVar[LookState] = LookState(
        GROUP,
        "an12-stopped",
        "Ответ остановлен кнопкой Stop после двадцати слов",
        "двадцать слов ответа и ниже курсивом «остановлено пользователем»; "
        "курсора и кружка нет",
        (TalkFixes.PLAIN,),
    )
    STOPPED_AGAIN: ClassVar[LookState] = LookState(
        GROUP,
        "an12-stopped-reloaded",
        "Тот же остановленный ответ после перезагрузки страницы",
        "кадр тот же, что «Ответ остановлен», с пометкой «остановлено "
        "пользователем»; тест сверил его с живым пиксель в пиксель",
        (TalkFixes.RELOAD,),
    )
    WAITING: ClassVar[LookState] = LookState(
        GROUP,
        "tr1-waiting",
        "Вопрос принят, модель ещё не ответила ни одного токена",
        "под вопросом — только мигающий кружок с аватаром ассистента; строки "
        "хода «Запрос отправлен…» больше нет",
        (TalkFixes.CONTAINER,),
    )
    PULSE_AFTER_TOOL: ClassVar[LookState] = LookState(
        GROUP,
        "tr5-pulse-after-tool",
        "Вызов отработал, модель думает над ответом",
        "текст «Смотрю подключения.», под ним свёрнутая строка хода с "
        "текущим действием «✓ connection_list · Смотрю подключения · 0 мс» "
        "(мерцание идущего хода в кадре заморожено), ниже мигающий кружок — "
        "ход ещё идёт",
        (TalkFixes.DONE, TalkFixes.CURRENT),
    )
    RUNNING_LONG: ClassVar[LookState] = LookState(
        GROUP,
        "tr10-running-long",
        "Ход идёт, подпись вызова длиннее строки",
        "текст «Проверяю источники.», под ним свёрнутая строка хода в одну "
        "строку: «✓ connection_list · Перебираю все подключения…» обрезана "
        "многоточием у края ленты, прокрутки вбок нет; ниже мигающий кружок",
        (TalkFixes.CURRENT,),
    )
    DONE: ClassVar[LookState] = LookState(
        GROUP,
        "tr2-done",
        "Ход с вызовом закончен: текст, строка хода свёрнута, ответ",
        "«Смотрю подключения.», под ним свёрнутая строка «Завершено · шагов: "
        "1», ниже итоговый ответ; кружка нет",
        (TalkFixes.DONE, TalkFixes.PLAIN),
    )
    OPENED: ClassVar[LookState] = LookState(
        GROUP,
        "tr3-opened",
        "Строка хода раскрыта: шаг вызова под ней",
        "под «Завершено · шагов: 1» шаг вызова с подписью «Смотрю "
        "подключения» и левой чертой цвета темы",
        (TalkFixes.PLAIN,),
    )
    DONE_AGAIN: ClassVar[LookState] = LookState(
        GROUP,
        "tr2-done-reloaded",
        "Тот же ход с вызовом после перезагрузки страницы",
        "кадр тот же, что «Ход с вызовом закончен»: строка хода между текстом "
        "и ответом; тест сверил его с живым пиксель в пиксель",
        (TalkFixes.CONTAINER, TalkFixes.RELOAD),
    )
    THINKING: ClassVar[LookState] = LookState(
        GROUP,
        "tr7-thinking",
        "Модель размышляет: пятнадцать слов рассуждения",
        "строка хода «○ Размышления» — подпись текущего шага — раскрыта, в ней "
        "шаг «Размышления» с кружком «идёт» и первыми пятнадцатью словами "
        "рассуждения; мигающий кружок в конце ленты",
        (TalkFixes.CONTAINER, TalkFixes.CURRENT),
    )
    THOUGHT: ClassVar[LookState] = LookState(
        GROUP,
        "tr8-thought",
        "Размышления закончены, ход закончен",
        "свёрнутая строка «Завершено · шагов: 1», под ней ответ",
        (TalkFixes.DONE,),
    )
    THOUGHT_OPEN: ClassVar[LookState] = LookState(
        GROUP,
        "tr9-thought-open",
        "Строка хода и размышления раскрыты: рассуждение на шестьдесят слов",
        "шаг «✓ Размышления» с галочкой, текст рассуждения переносится по "
        "словам и читается в обеих темах",
        (TalkFixes.THOUGHT,),
    )

    LOADING: ClassVar[LookState] = LookState(
        GROUP,
        "tl1-history-loading",
        "Тред открыт заново, история ещё загружается",
        "вопрос и ответ из истории, под ними по центру крутящийся значок и "
        "«Загружаем беседу…»; кружка ожидания и строки хода нет",
        (TalkFixes.LOADING,),
    )
    LOADING_COMPOSER: ClassVar[LookState] = LookState(
        GROUP,
        "tl2-history-loading-composer",
        "Поле ввода, пока история треда загружается",
        "кнопка отправки неактивна (стрелка, бледная), кнопки «Стоп» нет",
        (TalkFixes.LOADING,),
    )

    FEED: ClassVar[str] = LookPages.FEED
    SCROLLER: ClassVar[str] = (
        "xpath=ancestor::div"
        "[contains(concat(' ', @class, ' '), ' overflow-y-auto ')][1]"
    )
    """Сама область прокрутки сообщений."""
    END: ClassVar[str] = "(node) => { node.scrollTop = node.scrollHeight; }"
    LOGIN_USER: ClassVar[str] = "admin"
    UPLOAD: ClassVar[str] = "#upload-button-input"
    COMPOSER_BOX: ClassVar[str] = "#message-composer"
    ATTACHMENTS: ClassVar[str] = "#attachments > *"
    STOP: ClassVar[str] = "#stop-button"
    HISTORY_LOADING: ClassVar[str] = "#history-loading"
    SOCKETS: ClassVar[str] = "**/ws/socket.io/**"
    HISTORY_LOADED: ClassVar[str] = '"history_loaded"'
    """Событие конца загрузки истории в кадре socket.io: кадр с ним тест
    вида не пропускает вкладке, и загрузка на экране не кончается."""
    EDIT: ClassVar[str] = ".edit-message"
    EDIT_INPUT: ClassVar[str] = "#edit-chat-input"
    CONFIRM_EDIT: ClassVar[str] = ".confirm-edit"
    PULSE: ClassVar[str] = ".loading-cursor"
    SCROLL_DOWN: ClassVar[str] = "button:has(svg.lucide-arrow-down)"
    TABLE_BOX: ClassVar[str] = "table"
    CODE_BOX: ClassVar[str] = "pre"
    TALL: ClassVar[int] = 3600
    """Высота окна кадра ответа-документа целиком."""
    TURN_SEC: ClassVar[float] = 120.0
    HELD_SEC: ClassVar[float] = 60.0


class TalkFeed:
    """Лента разговора глазами теста вида: вопросы книге ответов, ожидание
    конца хода по журналу сокета, область ленты, прокрученная к вопросу хода,
    и тот же тред, открытый заново. Ошибки страницы копятся с открытия."""

    def __init__(self, page: Page, stand: StandProcess, book: ReplyBookClient) -> None:
        self._page = page
        self._log = SocketLog()
        self._book = book
        self._chat = ChatPage(page=page, log=self._log, base_url=stand.config.base_url)
        self.errors: list[str] = []
        page.on("pageerror", self._failed)
        page.on("websocket", self._watch)

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    def _watch(self, socket: WebSocket) -> None:
        socket.on("framereceived", self._log.accept)

    @property
    def page(self) -> Page:
        return self._page

    @property
    def chat(self) -> ChatPage:
        return self._chat

    def ask(self, question: TalkQuestion) -> None:
        """Ход целиком: вопрос и конец хода."""
        self._chat.ask(question.value)
        self._chat.await_idle(TalkLook.TURN_SEC)

    def ask_held(self, question: TalkQuestion) -> None:
        """Вопрос, чей поток книга держит на паузе: тест ждёт паузу."""
        self._chat.ask(question.value)
        self._book.await_held(question.value, TalkLook.HELD_SEC)

    def release(self, question: TalkQuestion) -> None:
        """Отпускает паузу потока и ждёт конца хода."""
        self._book.release(question.value)
        self._chat.await_idle(TalkLook.TURN_SEC)

    def stop(self, question: TalkQuestion) -> None:
        """Кнопка Stop посреди потока на паузе: ход кончается остановкой,
        пауза книги снимается, чтобы поток не висел."""
        self._page.locator(TalkLook.STOP).click()
        self._chat.await_idle(TalkLook.TURN_SEC)
        self._book.release(question.value)

    def asked(self, question: TalkQuestion) -> Locator:
        """Сообщение вопроса в ленте: последнее с первой строкой вопроса —
        как её показывает разметка, без обратных кавычек кода."""
        first = question.value.split("\n")[0].replace("`", "")
        return self._page.locator(
            Selector.of_type(StepKind.USER.value), has_text=first
        ).last

    def feed(self, question: TalkQuestion) -> Locator:
        """Лента с кнопкой «↓», прокрученная к вопросу хода."""
        asked = self.asked(question)
        asked.evaluate(LookPages.TOP)
        return asked.locator(TalkLook.FEED)

    def to_end(self, question: TalkQuestion) -> Locator:
        """Лента, прокрученная до конца."""
        asked = self.asked(question)
        asked.locator(TalkLook.SCROLLER).evaluate(TalkLook.END)
        return asked.locator(TalkLook.FEED)

    def tall(self, size: LookSize) -> ViewportSize:
        """Окно той же ширины, в которое ответ-документ входит целиком."""
        return {"width": size.viewport["width"], "height": TalkLook.TALL}

    def attached(self, names: Sequence[str]) -> Locator:
        """Плашки вложений поля ввода, когда загружены все names: у картинки
        имя — подпись миниатюры, у прочих — текст плашки."""
        composer = self._page.locator(TalkLook.COMPOSER_BOX)
        plates = composer.locator(TalkLook.ATTACHMENTS)
        expect(plates).to_have_count(len(names), timeout=30_000)
        for name in names:
            plate = plates.filter(has=self._page.get_by_alt_text(name))
            named = plates.filter(has_text=name)
            expect(plate.or_(named)).to_have_count(1, timeout=30_000)

        return composer

    def editing(self, answer: TalkText) -> Locator:
        """Лента с полем правки вопроса, прокрученная к нему: пузыря вопроса
        на время правки нет, лента находится от ответа хода."""
        edited = self._page.locator(Selector.of_type(StepKind.USER.value)).filter(
            has=self._page.locator(TalkLook.EDIT_INPUT)
        )
        edited.evaluate(LookPages.TOP)
        answered = self._page.locator(
            Selector.of_type(StepKind.ASSISTANT.value), has_text=answer
        ).last
        return answered.locator(TalkLook.FEED)

    def answer(self) -> Locator:
        return self._page.locator(Selector.of_type(StepKind.ASSISTANT.value)).last

    def shown(self, text: str) -> None:
        """Текст text нарисован в ленте."""
        expect(self._page.get_by_text(text).last).to_be_visible(timeout=60_000)

    def no_pulse(self) -> None:
        """Ход закончен: кружка ожидания нет."""
        expect(self._page.locator(TalkLook.PULSE)).to_have_count(0)

    def pulse(self) -> None:
        """Ход ждёт: кружок ожидания нарисован."""
        expect(self._page.locator(TalkLook.PULSE).last).to_be_visible(timeout=60_000)

    def open_last(self, kind: StepKind) -> None:
        """Раскрывает последний шаг вида kind, если он свёрнут."""
        self._toggle_last(kind, opened=True)

    def fold_last(self, kind: StepKind) -> None:
        """Сворачивает последний шаг вида kind, если он раскрыт."""
        self._toggle_last(kind, opened=False)

    def _toggle_last(self, kind: StepKind, opened: bool) -> None:
        step = self._page.locator(Selector.of_type(kind.value)).last
        toggle = step.locator(Selector.STEP_TOGGLE.value).first
        toggle.wait_for(timeout=60_000)
        state = toggle.get_attribute(Selector.TOGGLE_STATE.value)
        if (state == Selector.OPENED.value) == opened:
            return

        toggle.click()
        self._page.wait_for_timeout(300)

    def reload(self) -> None:
        """Тот же тред, открытый заново: лента собирается из истории агента."""
        thread_id = self._log.thread_id()
        self._page.goto(
            f"{self._chat.base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        self._page.locator(Selector.INPUT.value).wait_for(timeout=60_000)
        self._page.wait_for_load_state("networkidle")

    def reload_loading(self) -> None:
        """Тот же тред, открытый заново, но конец загрузки истории вкладке не
        приходит: сокет идёт через перехват, который выбрасывает событие
        history_loaded, — экран остаётся в состоянии загрузки."""
        self._page.route_web_socket(TalkLook.SOCKETS, self._hold_loading)
        self.reload()

    @staticmethod
    def _hold_loading(socket: WebSocketRoute) -> None:
        server = socket.connect_to_server()

        def from_server(message: str | bytes) -> None:
            if isinstance(message, str) and TalkLook.HISTORY_LOADED in message:
                return

            socket.send(message)

        server.on_message(from_server)

    def check_clean(self) -> None:
        if self.errors:
            raise LookError(f"the chat page raised errors: {self.errors}")


class TalkFiles:
    """Вложения вопроса: картинка, PDF и таблица, собранные в каталоге теста."""

    NAMES: ClassVar[tuple[str, str, str]] = ("chart.png", "report.pdf", "table.csv")
    CSV: ClassVar[str] = "регион,сумма\nСевер,3950\nЮг,6140\n"
    BARS: ClassVar[tuple[tuple[int, int, int], ...]] = (
        (0, 120, 210),
        (230, 120, 30),
        (40, 160, 90),
    )

    def __init__(self, root: Path) -> None:
        self._root = root

    def paths(self) -> list[str]:
        self._root.mkdir(parents=True, exist_ok=True)
        chart = self._root / self.NAMES[0]
        chart.write_bytes(self._chart())
        report = self._root / self.NAMES[1]
        Image.new("RGB", (120, 160), (255, 255, 255)).save(report, "PDF")
        table = self._root / self.NAMES[2]
        table.write_text(self.CSV, encoding="utf-8")
        return [str(chart), str(report), str(table)]

    def _chart(self) -> bytes:
        """Столбчатый график 480×300: три столбца на белом, размер обычного
        снимка экрана, а не значка."""
        image = Image.new("RGB", (480, 300), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        for index, color in enumerate(self.BARS):
            left = 60 + index * 130
            top = 250 - (index + 1) * 70
            draw.rectangle((left, top, left + 90, 270), fill=color)

        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()


@pytest.fixture(scope="module")
def talk_stand(look_stands: LookStand) -> StandProcess:
    return look_stands.of(StandAuth.LOCAL)


@pytest.fixture
def talk_book(look_llm_port: int) -> ReplyBookClient:
    return ReplyBookClient(StandUrl.of(look_llm_port))


def talk_frames(
    look_pages: LookPages, stand: StandProcess, book: ReplyBookClient
) -> Iterator[tuple[LookTheme, LookSize, TalkFeed]]:
    """Сочетания темы и размера: свой вход, свой тред и заново положенная
    книга — паузы потока держат каждый проход."""
    replies = TalkBook().replies()
    for size in LookSize:
        for theme in LookTheme:
            book.register(replies)
            page = look_pages.open(theme, size)
            # журнал сокета слушает с открытия: сокет поднимает уже вход
            feed = TalkFeed(page, stand, book)
            look_pages.sign_in(page, stand, TalkLook.LOGIN_USER)
            yield theme, size, feed


def test_user_question_look(
    look: LookCheck,
    look_pages: LookPages,
    talk_stand: StandProcess,
    talk_book: ReplyBookClient,
    tmp_path: Path,
) -> None:
    files = TalkFiles(tmp_path / "files").paths()
    for theme, size, feed in talk_frames(look_pages, talk_stand, talk_book):
        page = feed.page
        feed.ask(TalkQuestion.STATUS)
        feed.shown(TalkText.STATUS)
        feed.no_pulse()
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.STATUS), TalkLook.SHORT, theme, size)

        feed.ask(TalkQuestion.COMPARE)
        feed.shown(TalkText.COMPARE)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.COMPARE), TalkLook.LONG, theme, size)

        feed.ask(TalkQuestion.WHY)
        feed.shown("Перечислите колонки явно.")
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.WHY), TalkLook.MARKDOWN, theme, size)

        page.set_input_files(TalkLook.UPLOAD, files)
        composer = feed.attached(TalkFiles.NAMES)
        page.locator(Selector.INPUT.value).fill(TalkQuestion.FILES.value)
        page.wait_for_load_state("networkidle")
        look_pages.settle(page)
        look.frame(composer, TalkLook.COMPOSER, theme, size)
        page.locator(Selector.SUBMIT.value).click()
        feed.chat.await_idle(TalkLook.TURN_SEC)
        feed.shown(TalkText.FILES)
        asked = feed.asked(TalkQuestion.FILES)
        expect(asked.locator("img")).to_have_count(1)
        expect(asked.locator("img").first).to_have_js_property("complete", True)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.FILES), TalkLook.ATTACHED, theme, size)

        feed.ask(TalkQuestion.DAY)
        feed.shown(TalkText.DAY)
        asked = feed.asked(TalkQuestion.DAY)
        asked.locator(TalkLook.EDIT).click(force=True)
        page.locator(TalkLook.EDIT_INPUT).fill(TalkQuestion.WEEK.value)
        look_pages.settle(page)
        look.frame(feed.editing(TalkText.DAY), TalkLook.EDITING, theme, size)
        feed.chat.log.clear()
        page.locator(TalkLook.CONFIRM_EDIT).click()
        feed.chat.await_idle(TalkLook.TURN_SEC)
        feed.shown(TalkText.WEEK)
        expect(page.get_by_text(TalkText.DAY)).to_have_count(0)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.WEEK), TalkLook.EDITED, theme, size)
        feed.check_clean()

    look.verify()


def test_model_answer_look(
    look: LookCheck,
    look_pages: LookPages,
    talk_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    book = TalkBook()
    for theme, size, feed in talk_frames(look_pages, talk_stand, talk_book):
        page = feed.page
        feed.ask(TalkQuestion.REPORT)
        feed.shown("Данные за сентябрь предварительные.")
        expect(feed.answer().locator("pre code span").first).to_be_visible()
        look_pages.settle(page)
        document = feed.answer()
        # документ выше окна: кадр целиком — на высоком окне, затем окно прежнее
        page.set_viewport_size(feed.tall(size))
        look.frame(document, TalkLook.DOCUMENT, theme, size)
        page.set_viewport_size(size.viewport)
        look.frame(
            document.locator(TalkLook.TABLE_BOX).first, TalkLook.TABLE, theme, size
        )
        sql = document.locator(TalkLook.CODE_BOX).nth(1)
        look.frame(sql, TalkLook.CODE, theme, size)

        feed.ask(TalkQuestion.MARGIN)
        feed.shown("остаётся текстом.")
        expect(feed.answer().locator(".katex").first).to_be_visible()
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.MARGIN), TalkLook.FORMULAS, theme, size)

        feed.ask(TalkQuestion.HISTORY)
        feed.shown("Этап 40.")
        top = feed.feed(TalkQuestion.HISTORY)
        expect(top.locator(TalkLook.SCROLL_DOWN)).to_be_visible(timeout=10_000)
        look_pages.settle(page)
        look.frame(top, TalkLook.HISTORY_TOP, theme, size)
        end = feed.to_end(TalkQuestion.HISTORY)
        expect(end.locator(TalkLook.SCROLL_DOWN)).to_have_count(0, timeout=10_000)
        look_pages.settle(page)
        look.frame(end, TalkLook.HISTORY_END, theme, size)

        streamed = book.streamed(
            book.words(80, TalkBook.STEPS_SEED), TalkBook.STEPS_TOKENS
        )
        feed.ask_held(TalkQuestion.STEPS)
        expect(feed.answer()).to_contain_text(streamed, timeout=30_000)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.STEPS), TalkLook.STREAMING, theme, size)
        feed.release(TalkQuestion.STEPS)

        stopped = book.streamed(
            book.words(400, TalkBook.PLAN_SEED), TalkBook.PLAN_TOKENS
        )
        feed.ask_held(TalkQuestion.PLAN)
        expect(feed.answer()).to_contain_text(stopped, timeout=30_000)
        feed.stop(TalkQuestion.PLAN)
        feed.shown("остановлено пользователем")
        feed.no_pulse()
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.PLAN), TalkLook.STOPPED, theme, size)

        feed.reload()
        feed.shown("остановлено пользователем")
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.PLAN), TalkLook.STOPPED_AGAIN, theme, size)
        look.same_as(TalkLook.STOPPED_AGAIN, TalkLook.STOPPED, theme, size)
        feed.check_clean()

    look.verify()


def test_turn_look(
    look: LookCheck,
    look_pages: LookPages,
    talk_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    book = TalkBook()
    for theme, size, feed in talk_frames(look_pages, talk_stand, talk_book):
        page = feed.page
        feed.ask_held(TalkQuestion.STORAGE)
        feed.pulse()
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.STORAGE), TalkLook.WAITING, theme, size)
        feed.release(TalkQuestion.STORAGE)
        feed.shown(TalkText.STORAGE)

        reasoning = book.streamed(book.thinking(), TalkBook.THINKING_TOKENS)
        feed.ask_held(TalkQuestion.PARTITIONS)
        feed.open_last(StepKind.RUN)
        feed.open_last(StepKind.LLM)
        thought = page.locator(Selector.of_type(StepKind.LLM.value)).last
        expect(thought).to_contain_text(reasoning, timeout=30_000)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.PARTITIONS), TalkLook.THINKING, theme, size)
        feed.release(TalkQuestion.PARTITIONS)
        feed.shown(TalkText.PARTITIONS)
        feed.no_pulse()
        feed.fold_last(StepKind.RUN)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.PARTITIONS), TalkLook.THOUGHT, theme, size)

        feed.open_last(StepKind.RUN)
        feed.open_last(StepKind.LLM)
        look_pages.settle(page)
        look.frame(
            feed.feed(TalkQuestion.PARTITIONS), TalkLook.THOUGHT_OPEN, theme, size
        )
        feed.fold_last(StepKind.RUN)

        feed.ask_held(TalkQuestion.CONNECTIONS)
        feed.shown(TalkText.LOOKING)
        expect(page.locator(Selector.of_type(StepKind.RUN.value)).last).to_be_visible()
        feed.pulse()
        look_pages.settle(page)
        look.frame(
            feed.feed(TalkQuestion.CONNECTIONS), TalkLook.PULSE_AFTER_TOOL, theme, size
        )
        feed.release(TalkQuestion.CONNECTIONS)
        feed.shown(TalkText.CONNECTIONS)
        feed.no_pulse()
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.CONNECTIONS), TalkLook.DONE, theme, size)

        feed.reload()
        feed.shown(TalkText.CONNECTIONS)
        look_pages.settle(page)
        look.frame(
            feed.feed(TalkQuestion.CONNECTIONS), TalkLook.DONE_AGAIN, theme, size
        )
        look.same_as(TalkLook.DONE_AGAIN, TalkLook.DONE, theme, size)

        feed.open_last(StepKind.RUN)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.CONNECTIONS), TalkLook.OPENED, theme, size)
        feed.check_clean()

    look.verify()


def test_history_loading_look(
    look: LookCheck,
    look_pages: LookPages,
    talk_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    for theme, size, feed in talk_frames(look_pages, talk_stand, talk_book):
        page = feed.page
        feed.ask(TalkQuestion.STATUS)
        feed.shown(TalkText.STATUS)

        feed.reload_loading()
        feed.shown(TalkText.STATUS)
        expect(page.locator(TalkLook.HISTORY_LOADING)).to_be_visible()
        expect(page.locator(Selector.SUBMIT.value)).to_be_disabled()
        expect(page.locator(TalkLook.STOP)).to_have_count(0)
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.STATUS), TalkLook.LOADING, theme, size)
        composer = page.locator(TalkLook.COMPOSER_BOX)
        look.frame(composer, TalkLook.LOADING_COMPOSER, theme, size)
        feed.check_clean()

    look.verify()


def test_running_turn_look(
    look: LookCheck,
    look_pages: LookPages,
    talk_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    for theme, size, feed in talk_frames(look_pages, talk_stand, talk_book):
        page = feed.page
        feed.ask_held(TalkQuestion.SOURCES)
        feed.shown(TalkText.CHECKING)
        running = page.locator(Selector.of_type(StepKind.RUN.value)).last
        expect(running).to_contain_text(TalkBook.LONG_INTENT, timeout=30_000)
        feed.pulse()
        look_pages.settle(page)
        look.frame(feed.feed(TalkQuestion.SOURCES), TalkLook.RUNNING_LONG, theme, size)
        feed.release(TalkQuestion.SOURCES)
        feed.shown(TalkText.SOURCES)
        feed.check_clean()

    look.verify()
