from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, MutableSequence, Sequence
from types import TracebackType
from typing import Generic, Self, TypeVar
from uuid import UUID, uuid4

TName = TypeVar("TName")


class Id(ABC, Generic[TName]):
    """Базовый value object для идентификаторов."""

    def __init__(self, name: TName) -> None:
        self._name = name

    @property
    def name(self) -> TName:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._name!r})"

    @abstractmethod
    def to_wire(self) -> str:
        """Сериализовать Id в строковое представление (JSON-safe)."""
        ...

    @classmethod
    @abstractmethod
    def from_wire(cls, value: str) -> Self:
        """Восстановить Id из строкового представления."""
        ...


class UuId(Id[UUID]):
    """Базовый UUID-идентификатор."""

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    @classmethod
    def from_uuid(cls, _id: UUID) -> Self:
        return cls(_id)

    def to_wire(self) -> str:
        return str(self._name)

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(UUID(value))


TCtx = TypeVar("TCtx")
TIn = TypeVar("TIn")
TId = TypeVar("TId", bound=Id)
TKey = TypeVar("TKey")
TState = TypeVar("TState")
TOut = TypeVar("TOut")
TValue = TypeVar("TValue")
TQuery = TypeVar("TQuery")


class StateLess(ABC):
    """
    Признак того, что объект не имеет внутреннего состояния
    не зависит от порядка вызовов
    """

    @abstractmethod
    def name(self) -> str: ...


class StateFull(StateLess):
    """
    Признак того, что объект имеет внутреннее состояние
    зависит от порядка вызовов
    """

    def reset(self) -> None:
        pass


class Validator(ABC, Generic[TValue]):
    """
    Валидатор + нормализатор.
    Принимает значение, возвращает нормализованное.
    Бросает исключение, если значение невалидно.
    """

    @abstractmethod
    def validate(self, value: TValue) -> TValue: ...


class ConverterError(Exception):
    """Базовая ошибка ``Converter.convert``.

    Реализации ``Converter`` ОБЯЗАНЫ оборачивать любые внутренние исключения
    в потомков этого класса. Голые ``ValueError``/``KeyError``/``TypeError``
    и т.п. наружу утекать не должны — потребитель ловит только
    ``ConverterError``.
    """


class ConverterInputError(ConverterError):
    """Вход не соответствует ожидаемому формату или семантике.

    Покрывает: битый синтаксис, отсутствующие/лишние поля, недопустимые
    значения, неизвестные варианты перечислений и т.п. Обычно «проблема
    данных», которую можно залогировать и пропустить.
    """


class ConverterOutputError(ConverterError):
    """Не удалось построить выход из корректного входа.

    Обычно указывает на баг в реализации конвертера (неожиданное поведение
    библиотеки сериализации, неподдерживаемый тип в структуре и т.п.).
    """


class Converter(ABC, Generic[TIn, TOut]):
    """Однонаправленная конвертация 1:1. A → B.

    Контракт ошибок: ``convert`` бросает только потомков ``ConverterError``.
    ``ConverterInputError`` — если проблема в данных входа;
    ``ConverterOutputError`` — если проблема в построении выхода.
    Исходное низкоуровневое исключение доступно через ``__cause__``.
    """

    @abstractmethod
    def convert(self, value: TIn) -> TOut:
        """Выполнить конвертацию.

        Raises:
            ConverterInputError: вход не соответствует контракту.
            ConverterOutputError: невозможно сформировать выход.
        """
        ...


class StreamConverter(ABC, Generic[TIn, TOut]):
    """
    Потоковая конвертация N:M. Iterable[A] → Iterable[B].
    """

    @abstractmethod
    def convert(self, stream: Iterable[TIn]) -> Iterable[TOut]: ...


class StreamSource(StateFull, Generic[TCtx, TOut]):
    """
    Источник потока событий.

    Генерирует ``Iterable[TOut]``, опираясь только на ``ctx`` и
    зависимости, внедрённые в конструктор. Входного потока у источника
    нет; если он нужен — это уже :class:`StreamTransformer`.

    Схема::

                         ctx
                          │
                          ▼
                  ┌──────────────┐
                  │    source    │ ──▶ Iterable[TOut]
                  └──────────────┘

    Контракт:
    - :meth:`stream` возвращает генератор — события выдаются лениво,
      по мере готовности; побочные эффекты допустимы (подготовить
      ``ctx.llm_builder``, отправить запрос, открыть файл);
    - ``StateFull`` — реализация может копить состояние между
      итерациями (флаг «replay уже выполнен», счётчики);
      :meth:`reset` возвращает состояние к начальному.

    Когда использовать:
    - middleware в агентской цепочке: стадия читает/мутирует ``ctx``,
      эмитит собственные события и делегирует inner'у. Onion-композиция
      собирается через :class:`StreamSourceChainBuilder`;
    - терминальный поставщик событий (LLM-клиент, подписка на очередь,
      обход файлов);
    - несколько независимых источников на одном ``ctx``, чьи события
      нужно слить — :class:`StreamSourcePipeline`;
    - циклический запуск до условия остановки —
      :class:`StreamSourceLoop`.

    Когда НЕ использовать:
    - если на вход приходит реальный поток данных, который нужно
      преобразовать — см. :class:`StreamTransformer`;
    - если стадия только потребляет события и совершает побочный
      эффект без возврата — см. :class:`StreamSink`.
    """

    @abstractmethod
    def stream(self, ctx: TCtx) -> Iterable[TOut]: ...


class StreamSink(StateFull, Generic[TCtx, TIn]):
    """
    Терминальный потребитель событий.

    Обрабатывает одно событие за вызов и совершает побочные эффекты.
    Выходного потока нет — это край pipeline'а, дальше делегировать
    нечему.

    Схема::

                     ctx, event
                          │
                          ▼
                  ┌──────────────┐
                  │     sink     │ ──▶ (side effects)
                  └──────────────┘

    Типовая точка вызова::

        for event in source.stream(ctx):
            sink.handle(ctx, event)

    Контракт:
    - :meth:`handle` синхронно обрабатывает одно событие и ничего не
      возвращает; реализация может копить состояние между вызовами
      (буферы для агрегирования стриминговых токенов в «завершённые»
      сообщения, счётчики, дебаунсеры);
    - ``StateFull`` — :meth:`reset` очищает накопленные буферы.

    Когда использовать:
    - вывод в консоль/UI, запись в журнал, отправка наружу
      (webhook, message bus);
    - агрегация стриминга в «завершённые» сущности (пример —
      HistorySink: токены → ``*Complete`` в журнал);
    - fan-out одного события на несколько потребителей сразу
      (консоль + журнал + телеметрия) — :class:`StreamSinkPipeline`.

    Когда НЕ использовать:
    - если стадия должна что-то вернуть обратно в поток — это уже
      обёртка источника, см. :class:`StreamSource` (через inner
      в onion-цепочке);
    - если есть вход-поток и выход-поток — :class:`StreamTransformer`.
    """

    @abstractmethod
    def handle(self, ctx: TCtx, event: TIn) -> None: ...


class StreamTransformer(StateFull, Generic[TCtx, TIn, TOut]):
    """
    Потоковое преобразование с контекстом.

    Принимает ``Iterable[TIn]`` и выдаёт ``Iterable[TOut]`` в
    присутствии ``ctx``. Это «обычная» ETL-стадия: декодер формата,
    фильтр, разбиение по ключу, генерация событий из дельт, и т.п.

    Схема::

                  ctx, Iterable[TIn]
                          │
                          ▼
                  ┌──────────────┐
                  │  transformer │ ──▶ Iterable[TOut]
                  └──────────────┘

    Отличия от соседних паттернов:
    - от :class:`StreamConverter` — знает про ``ctx`` (request_id,
      workspace, config). Если ``ctx`` не нужен — берите
      ``StreamConverter``: он легче и явно заявляет отсутствие
      зависимости от контекста;
    - от :class:`StreamSource` — у трансформера есть значимый входной
      поток. Если вход всегда ``None`` — это источник;
    - от :class:`StreamSink` — возвращает выходной поток, а не только
      совершает побочный эффект.

    Контракт:
    - :meth:`stream` — лениво; если стадия внутри делает
      ``for item in stream`` и отдаёт одну-в-одну, стоит подумать,
      не :class:`Converter` ли это вообще;
    - ``StateFull`` — допустимо состояние между элементами входа
      (буфер для склейки дельт, флаги «первое появление»);
      :meth:`reset` сбрасывает его.

    Когда использовать:
    - ленивый декодер/кодек (OpenAI chunks → AgentEvent);
    - стадия, агрегирующая/разворачивающая поток и производящая
      производные события;
    - fan-out нескольких трансформеров на один вход с одним ``ctx`` —
      :class:`StreamTransformerPipeline`.
    """

    @abstractmethod
    def stream(self, ctx: TCtx, stream: Iterable[TIn]) -> Iterable[TOut]: ...


class Definition(ABC, Generic[TValue]):
    @abstractmethod
    def definition(self) -> TValue: ...


class Resolver(ABC, Generic[TIn, TOut]):
    @abstractmethod
    def resolve(self, req: TIn) -> TOut: ...


class Matcher(ABC, Generic[TQuery, TValue]):
    """
    Находит кандидатов в пуле по запросу.

    Чистая функция ``(query, pool) → candidates``. Не имеет состояния,
    не знает о хранилище — работает над любым ``Iterable[TValue]``.
    Применяется там, где «поиск» разделён с «хранением» и «ранжированием»:
    matcher фильтрует, ранкер упорядочивает, резолвер композирует.
    """

    @abstractmethod
    def match(self, query: TQuery, pool: Iterable[TValue]) -> Iterable[TValue]: ...


class Executor(ABC, Generic[TCtx, TIn, TOut]):
    """
    Команда-обработчик. Принимает запрос, возвращает результат.
    В отличие от Converter, может иметь побочные эффекты и отражает
    ошибки в типе TOut (discriminated union), а не через исключения
    """

    @abstractmethod
    def execute(self, ctx: TCtx, req: TIn) -> TOut: ...


class ChainError(Exception):
    """Базовая ошибка ``Chain``."""


class ChainConfigError(ChainError):
    """Некорректная конфигурация ``Chain`` (например, пустой список хэндлеров)."""


class ChainNoHandlersError(ChainError):
    """``Chain.execute`` не выполнил ни одного хэндлера.

    Инвариант: возникает только при повреждённом состоянии экземпляра
    (список хэндлеров опустел после инициализации). В штатном сценарии
    недостижимо.
    """


class FactoryMethod(ABC, Generic[TOut]):
    """
    Классический Factory: одношаговая сборка объекта.

    Инкапсулирует сложную логику конфигурирования внутри одного класса и
    отделяет создание объекта от его определения.

        [ build ] ──▶ out

    Когда использовать:
    - логика сборки нетривиальна, но выполняется «за один проход»
    - нужно спрятать детали конструирования от вызывающего кода

    Когда НЕ использовать:
    - если сборка распадается на независимые стадии, которые удобно
      регистрировать/снимать по отдельности — см. :class:`FoldFactory`.
    - если для сборки нужен внешний контекст, известный только в момент
      вызова — см. :class:`ContextFactoryMethod`.
    """

    @abstractmethod
    def build(self) -> TOut: ...


class ContextFactoryMethod(ABC, Generic[TCtx, TOut]):
    """
    Factory, которому для сборки нужен контекст вызова.

    Отличается от :class:`FactoryMethod` ровно одним: :meth:`build`
    принимает ``ctx: TCtx``. Ответ может меняться от вызова к вызову
    даже при одинаково сконфигурированной фабрике, потому что зависит
    от состояния, известного только в момент запроса.

        [ build(ctx) ] ──▶ out

    Когда использовать:
    - итоговый объект — проекция текущего состояния/запроса, а не
      статическая сборка (например, snapshot для одного LLM-вызова,
      per-request конфиг, запрос к внешнему API по параметрам ctx);
    - фабрика сама не держит изменяющиеся данные — они приходят снаружи
      через ``ctx``; это делает её stateless по отношению к бизнес-контексту.

    Когда НЕ использовать:
    - если для сборки не нужен внешний контекст — см. :class:`FactoryMethod`;
    - если сборка распадается на независимые стадии — см.
      :class:`FoldFactory` (статический) или будущий ``ContextFoldFactory``.
    """

    @abstractmethod
    def build(self, ctx: TCtx) -> TOut: ...


class PrioritySource(ABC, Generic[TId, TState]):
    """
    Одна стадия сборки для :class:`FoldFactory`.

    Reducer — это чистое преобразование состояния:

        state_n ──[ apply(state) ]──▶ state_{n+1}

    Каждый reducer:
    - имеет уникальный :meth:`id` — по нему его можно зарегистрировать
      или снять с регистрации в фабрике;
    - имеет :meth:`priority` — определяет порядок применения
      (меньше число — раньше выполнение);
    - в :meth:`apply` получает текущее состояние и возвращает новое.

    Логика одного reducer'а должна быть изолированной: он не знает
    ни о других стадиях, ни о финальном результате сборки. Для стадий,
    которым нужен внешний контекст — см. :class:`ContextPrioritySource`.
    """

    @abstractmethod
    def id(self) -> TId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def apply(self, state: TState) -> TState: ...


class FoldFactory(
    FactoryMethod[TOut],
    Generic[TId, TState, TOut],
):
    """
    :class:`FactoryMethod`, собирающий объект через последовательность
    стадий (fold).

    В отличие от обычного :class:`FactoryMethod`, здесь сборка разбита на
    независимые :class:`PrioritySource`-ы, которые регистрируются
    отдельно и применяются в порядке приоритета. Это удобно, когда набор
    стадий зависит от конфигурации, плагинов или окружения.

    Схема работы ``build()``::

                      ┌──────────────┐
                      │   initial    │  начальное состояние
                      └──────┬───────┘
                             │ state₀
                             ▼
                      ┌──────────────┐
                      │  reducer #1  │  priority = 10
                      └──────┬───────┘
                             │ state₁
                             ▼
                      ┌──────────────┐
                      │  reducer #2  │  priority = 20
                      └──────┬───────┘
                             │ state₂
                             ▼
                            ...
                             │ stateₙ
                             ▼
                      ┌──────────────┐
                      │   finalize   │  собираем результат
                      └──────┬───────┘
                             │
                             ▼
                            out

    Контракт наследника:
    - :meth:`initial`  — построить начальное состояние;
    - :meth:`finalize` — превратить накопленное состояние в результат;
    - стадии подключаются извне через :meth:`register` /
      :meth:`unregister`.

    Гарантии:
    - стадии применяются строго по возрастанию ``priority()``;
    - повторный :meth:`register` с тем же ``id()`` заменяет предыдущий
      reducer — это официальный способ переопределить стадию.

    Для сборки, зависящей от внешнего контекста —
    см. :class:`ContextFoldFactory`.
    """

    def __init__(self) -> None:
        self._reducers: dict[TId, PrioritySource[TId, TState]] = {}

    def register(self, reducer: PrioritySource[TId, TState]) -> None:
        self._reducers[reducer.id()] = reducer

    def unregister(self, key: TId) -> None:
        self._reducers.pop(key, None)

    def providers(self) -> Iterable[PrioritySource[TId, TState]]:
        return iter(self._reducers.values())

    @abstractmethod
    def initial(self) -> TState:
        """Начальное состояние сборки."""
        ...

    @abstractmethod
    def finalize(self, state: TState) -> TOut:
        """Превратить накопленное состояние в результат."""
        ...

    def build(self) -> TOut:
        state = self.initial()

        for p in sorted(self._reducers.values(), key=lambda p: p.priority()):
            state = p.apply(state)

        return self.finalize(state)


class DispatcherKeyError(KeyError):
    """Ключ не зарегистрирован в Dispatcher."""


class Dispatcher(Generic[TId, TValue]):
    """
    Находит хэндлер по ключу (или бросает исключение).

    Fan-in select:
    ``register(key, handler)``: кладёт handler во внутреннее хранилище
    ``unregister(self, key: TId)``: удаляет handler из внутреннего хранилища по ключу
    ``dispatch(key)`` возвращает найденного хэндлера по ключу

    Что делать с Что с ним делать (вызвать ``execute``, инспектировать metadata,
    обернуть в middleware) — дело вызывающего;
    Dispatcher не ассоциирует себя с ``UseCase`` и вообще не навязывает
    форму хэндлера.
    """

    def __init__(self) -> None:
        self._handlers: dict[TId, TValue] = {}

    def register(self, key: TId, handler: TValue) -> None:
        self._handlers[key] = handler

    def unregister(self, key: TId) -> None:
        self._handlers.pop(key, None)

    def dispatch(self, key: TId) -> TValue:
        handler = self._handlers.get(key)
        if handler is None:
            raise DispatcherKeyError(key)
        return handler

    def keys(self) -> Iterable[TId]:
        return iter(self._handlers.keys())

    def values(self) -> Iterable[TValue]:
        return iter(self._handlers.values())


class Serializer(Generic[TIn, TOut]):
    """
    Двусторонняя конвертация = композиция двух Converter.
        serialize() — прямое направление (encoder).
        deserialize() — обратное направление (decoder).
    """

    def __init__(
        self,
        encoder: Converter[TIn, TOut],
        decoder: Converter[TOut, TIn],
    ) -> None:
        self.encoder = encoder
        self.decoder = decoder

    def serialize(self, obj: TIn) -> TOut:
        return self.encoder.convert(obj)

    def deserialize(self, raw: TOut) -> TIn:
        return self.decoder.convert(raw)


class Specification(ABC, Generic[TValue]):
    """
    Бизнес-правило как объект.

    Проверяет, удовлетворяет ли кандидат условию через :meth:`check`.
    Правила можно комбинировать через :meth:`and_` / :meth:`or_` /
    :meth:`not_` — результат композиции тоже :class:`Specification`.
    """

    @abstractmethod
    def check(self, candidate: TValue) -> bool: ...

    def and_(self, other: Specification[TValue]) -> Specification[TValue]:
        return _AndSpec(self, other)

    def or_(self, other: Specification[TValue]) -> Specification[TValue]:
        return _OrSpec(self, other)

    def not_(self) -> Specification[TValue]:
        return _NotSpec(self)


class _AndSpec(Specification[TValue]):
    def __init__(
        self, left: Specification[TValue], right: Specification[TValue]
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: TValue) -> bool:
        return self._left.check(candidate) and self._right.check(candidate)


class _OrSpec(Specification[TValue]):
    def __init__(
        self, left: Specification[TValue], right: Specification[TValue]
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: TValue) -> bool:
        return self._left.check(candidate) or self._right.check(candidate)


class _NotSpec(Specification[TValue]):
    def __init__(self, spec: Specification[TValue]) -> None:
        self._spec = spec

    def check(self, candidate: TValue) -> bool:
        return not self._spec.check(candidate)


class ExceptionSpecification(Specification[Exception]):
    """
    Специализация :class:`Specification` для исключений.

    Отличается от базовой :class:`Specification` двумя вещами:

    1. Работает как context manager — можно фильтровать исключения без
       ручного ``try/except``, аналогично :func:`contextlib.suppress`,
       но с произвольным предикатом через :meth:`check`::

           transient = IsInstance(TimeoutError, ConnectionError)
           with transient:
               do_request()  # Timeout/ConnectionError — подавляются
                             # остальные — пробрасываются дальше

    2. Композиция :meth:`and_` / :meth:`or_` / :meth:`not_` остаётся
       в типе :class:`ExceptionSpecification`, поэтому собранную цепочку
       тоже можно использовать в ``with``.

    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) никогда не
    подавляется — даже если :meth:`check` вернёт True.
    """

    def and_(self, other: ExceptionSpecification) -> ExceptionSpecification:
        return _ExcAndSpec(self, other)

    def or_(self, other: ExceptionSpecification) -> ExceptionSpecification:
        return _ExcOrSpec(self, other)

    def not_(self) -> ExceptionSpecification:
        return _ExcNotSpec(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_value is None:
            return False
        if not isinstance(exc_value, Exception):
            return False
        return self.check(exc_value)


class _ExcAndSpec(ExceptionSpecification):
    def __init__(
        self, left: ExceptionSpecification, right: ExceptionSpecification
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: Exception) -> bool:
        return self._left.check(candidate) and self._right.check(candidate)


class _ExcOrSpec(ExceptionSpecification):
    def __init__(
        self, left: ExceptionSpecification, right: ExceptionSpecification
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: Exception) -> bool:
        return self._left.check(candidate) or self._right.check(candidate)


class _ExcNotSpec(ExceptionSpecification):
    def __init__(self, spec: ExceptionSpecification) -> None:
        self._spec = spec

    def check(self, candidate: Exception) -> bool:
        return not self._spec.check(candidate)


class IsInstance(ExceptionSpecification):
    """Проверка ``isinstance(exc, types)`` как :class:`ExceptionSpecification`.

    Мост между классическим ``except (A, B)`` и механизмом спецификаций.
    Результат можно использовать и как предикат через :meth:`check`,
    и как context manager через ``with``.

    Пример::

        transient = IsInstance(TimeoutError, ConnectionError)
        with transient:
            do_request()
    """

    def __init__(self, *types: type[Exception]) -> None:
        self._types = types

    def check(self, candidate: Exception) -> bool:
        return isinstance(candidate, self._types)


class StreamSourcePipeline(StreamSource[TCtx, TOut]):
    """
    Sequential fan-out композиция :class:`StreamSource`-ов в единый поток.

    Стадии выполняются строго в порядке регистрации. Каждая стадия
    получает общий ``ctx``; события всех стадий сливаются в выходной
    поток по мере появления (lazy, без буферизации). Стадии независимы
    друг от друга — предыдущая не видит и не влияет на следующую
    (в отличие от onion-цепочки из :class:`StreamSourceChainBuilder`).

    Схема::

                         ctx
                          │
                  ┌───────┴───────┐
                  ▼               │
              ┌────────┐          │
              │ src₁   │─▶ events₁ ┐
              └────────┘           │
                                   │
              ┌────────┐           ├──▶ общий поток TOut
              │ src₂   │─▶ events₂ ┤
              └────────┘           │
                                   │
                ...                │
              ┌────────┐           │
              │ srcₙ   │─▶ eventsₙ ┘
              └────────┘

    Обработка ошибок задаётся спецификацией ``continue_if_error``:
    - если не задана, исключение в любой стадии прерывает весь pipeline;
    - если задана, используется как context manager над стадией:
      исключение проверяется через ``continue_if_error.check``; при
      истинном предикате стадия пропускается, pipeline продолжает
      со следующей. Иначе исключение пробрасывается с исходным
      traceback.

    Примеры ``continue_if_error``:
    - ``IsInstance(TimeoutError, ConnectionError)`` — по типам (аналог
      ``except (TimeoutError, ConnectionError)``);
    - ``IsInstance(HTTPError).and_(IsTransientHTTPError())`` — по типу
      и дополнительному предикату над атрибутами исключения.

    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) никогда
    не перехватывается независимо от ``continue_if_error``.

    Когда использовать:
    - несколько независимых источников событий нужно слить в один
      поток на общем ``ctx`` (например, разные поставщики уведомлений);
    - стадии не должны оборачивать друг друга — у каждой свой
      самостоятельный жизненный цикл.

    Когда НЕ использовать:
    - стадии должны оборачивать следующую (middleware-цепочка) —
      :class:`StreamSourceChainBuilder`;
    - первая успешная стадия должна останавливать остальные
      (fallback) — этот паттерн в Stream-семье не реализован,
      ближайший аналог — :class:`ExecutorPipeline` на уровне
      Executor.

    :meth:`reset` сбрасывает состояние всех stateful-стадий;
    :meth:`name` возвращает читабельное имя вида
    ``SourcePipeline(a -> b -> c)``.
    """

    def __init__(
        self,
        stages: MutableSequence[StreamSource[TCtx, TOut]],
        continue_if_error: ExceptionSpecification | None = None,
    ) -> None:
        self._stages = stages
        self._continue_if_error = continue_if_error

    def append(self, stage: StreamSource[TCtx, TOut]):
        self._stages.append(stage)
        return self

    def stage_names(self) -> Iterable[str]:
        for s in self._stages:
            yield s.name()

    def name(self) -> str:
        return "SourcePipeline({})".format(" -> ".join(self.stage_names()))

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def stream(self, ctx: TCtx) -> Iterable[TOut]:
        if self._continue_if_error is None:
            for stage in self._stages:
                yield from stage.stream(ctx)
            return

        for stage in self._stages:
            with self._continue_if_error:
                yield from stage.stream(ctx)


class StreamSinkPipeline(StreamSink[TCtx, TIn]):
    """
    Broadcast-композиция :class:`StreamSink`-ов над одним событием.

    Каждый sink получает одно и то же событие в порядке регистрации.
    Это «тройник»: одно событие разводится по нескольким независимым
    потребителям (консоль + журнал + телеметрия) без необходимости
    их знать друг о друге.

    Схема::

                      ctx, event
                           │
                  ┌────────┼────────┐
                  ▼        ▼        ▼
              ┌───────┐┌───────┐┌───────┐
              │ sink₁ ││ sink₂ ││ sinkₙ │
              └───┬───┘└───┬───┘└───┬───┘
                  ▼        ▼        ▼
               side fx  side fx  side fx

    Вызовы строго последовательны в порядке регистрации (не
    параллельно): для пайплайна агентских sink'ов этого достаточно и
    сохраняет предсказуемый порядок побочных эффектов (консольный
    вывод появляется до записи в журнал).

    Обработка ошибок задаётся спецификацией ``continue_if_error``:
    - если не задана, исключение в любом sink'е прерывает обработку
      события, оставшиеся sink'и его не получат;
    - если задана, используется как context manager над каждым
      sink'ом: подходящие исключения подавляются, pipeline продолжает
      со следующим. Иначе исключение пробрасывается.

    ``BaseException`` никогда не перехватывается.

    Когда использовать:
    - одно событие нужно доставить в несколько независимых каналов;
    - каналы не должны знать друг о друге и не имеют общего состояния.

    Когда НЕ использовать:
    - sink'и должны вложенно оборачивать друг друга (например,
      метрики вокруг журналирования) — это onion-паттерн, но в
      Stream-семье реализован только на уровне источников
      (:class:`StreamSourceChainBuilder`); в sink-кейсе соберите
      middleware вручную.

    :meth:`reset` сбрасывает состояние всех stateful-стадий (в том
    числе буферы агрегации токенов); :meth:`name` — ``SinkPipeline(
    a -> b -> c)``.
    """

    def __init__(
        self,
        stages: MutableSequence[StreamSink[TCtx, TIn]],
        continue_if_error: ExceptionSpecification | None = None,
    ) -> None:
        self._stages = stages
        self._continue_if_error = continue_if_error

    def append(self, stage: StreamSink[TCtx, TIn]):
        self._stages.append(stage)
        return self

    def stage_names(self) -> Iterable[str]:
        for s in self._stages:
            yield s.name()

    def name(self) -> str:
        return "SinkPipeline({})".format(" -> ".join(self.stage_names()))

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def handle(self, ctx: TCtx, event: TIn) -> None:
        if self._continue_if_error is None:
            for stage in self._stages:
                stage.handle(ctx, event)
            return

        for stage in self._stages:
            with self._continue_if_error:
                stage.handle(ctx, event)


class StreamTransformerPipeline(StreamTransformer[TCtx, TIn, TOut]):
    """
    Sequential fan-out композиция :class:`StreamTransformer`-ов над
    одним входным потоком.

    Каждая стадия получает общий ``ctx`` и один и тот же входной
    ``stream``; события стадий сливаются в выходной поток по мере
    появления. Стадии выполняются в порядке регистрации и независимы
    друг от друга.

    Типовой кейс — один входной поток дельт декодируется несколькими
    специализированными извлекателями (``RoleSource`` + ``AnswerSource``
    + ``ToolCallSource``), каждый из которых смотрит на «свою» часть
    события.

    Схема::

                  ctx, Iterable[TIn]
                         │
                  ┌──────┴──────┐
                  ▼             │
              ┌────────┐        │
              │ xform₁ │──▶ events₁ ┐
              └────────┘            │
                                    │
              ┌────────┐            ├──▶ общий поток TOut
              │ xform₂ │──▶ events₂ ┤
              └────────┘            │
                                    │
                ...                 │
              ┌────────┐            │
              │ xformₙ │──▶ eventsₙ ┘
              └────────┘

    **Важно**: входной ``stream`` должен быть re-iterable (list,
    tuple, pydantic-поле). Pipeline НЕ материализует поток и передаёт
    один и тот же объект каждой стадии — если передать generator,
    вторая стадия увидит пустую последовательность. Это осознанный
    выбор: материализация в list «съела» бы ленивость там, где она не
    нужна, а дубликат через ``itertools.tee`` потребовал бы решений о
    размере буфера и семантике отмены. Ответственность — на
    вызывающем коде.

    Обработка ошибок устроена так же, как в
    :class:`StreamSourcePipeline`: ``continue_if_error`` как context
    manager над стадией, ``BaseException`` никогда не перехватывается.

    Когда использовать:
    - один входной поток нужно разложить на несколько производных
      потоков, которые затем слить обратно;
    - стадии-декодеры независимы и смотрят на разные аспекты
      входного элемента.

    Когда НЕ использовать:
    - стадии выстраиваются в pipeline «выход одной → вход следующей»
      (это обычная функциональная композиция, соберите её вручную
      через вложенные ``stream()``);
    - нужен fallback (первая успешная выигрывает) — этого в
      Stream-семье нет.

    :meth:`reset` сбрасывает состояние всех stateful-стадий;
    :meth:`name` — ``TransformerPipeline(a -> b -> c)``.
    """

    def __init__(
        self,
        stages: MutableSequence[StreamTransformer[TCtx, TIn, TOut]],
        continue_if_error: ExceptionSpecification | None = None,
    ) -> None:
        self._stages = stages
        self._continue_if_error = continue_if_error

    def append(self, stage: StreamTransformer[TCtx, TIn, TOut]):
        self._stages.append(stage)
        return self

    def stage_names(self) -> Iterable[str]:
        for s in self._stages:
            yield s.name()

    def name(self) -> str:
        return "TransformerPipeline({})".format(" -> ".join(self.stage_names()))

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def stream(self, ctx: TCtx, stream: Iterable[TIn]) -> Iterable[TOut]:
        if self._continue_if_error is None:
            for stage in self._stages:
                yield from stage.stream(ctx, stream)
            return

        for stage in self._stages:
            with self._continue_if_error:
                yield from stage.stream(ctx, stream)


class StreamSourceLoop(StreamSource[TCtx, TOut]):
    """
    Циклический запуск :class:`StreamSource` до срабатывания условия
    остановки.

    Оборачивает один источник и крутит его в бесконечном цикле: когда
    источник исчерпал свой поток, он вызывается снова на том же
    ``ctx`` — и так до тех пор, пока очередное событие не удовлетворит
    спецификации остановки ``stop_if``. Проверка выполняется после
    каждого yield'а, поэтому «финальное» событие гарантированно
    попадает в выходной поток — снаружи видно, на чём цикл
    остановился.

    Типовой кейс — агентский цикл: источник представляет одну
    итерацию «запрос в LLM + обработка tool_calls»; ``stop_if``
    срабатывает, когда LLM выдала финальный ответ (без tool_calls)
    или исчерпан лимит итераций.

    Схема::

                        ctx
                         │
                         ▼
                  ┌──────────────┐
            ┌──▶  │    source    │ ──▶ event ──▶ yield ──▶ наружу
            │     └──────┬───────┘                 │
            │            │                         │
            │   иссяк    │                         ▼
            │            │                ┌────────────────┐
            │            │         нет    │ stop_if.check  │
            └────────────┴──────────────  │  ((ctx, evt))  │
                                          └────────┬───────┘
                                                   │ да
                                                   ▼
                                                 STOP

    Контракт:
    - ``stop_if`` получает пару ``(ctx, event)`` и должен быть чистой
      проверкой без побочных эффектов;
    - проверка выполняется ПОСЛЕ ``yield``, так что «стоп-событие»
      всегда доставляется наружу (вызывающий видит, что его привело
      к остановке);
    - если источник всегда пуст, а условие никогда не срабатывает,
      цикл крутится вхолостую — ответственность за достижимость
      остановки лежит на вызывающем коде (обычно достаточно
      добавить страховочную спецификацию (например, реагирующую на
      ``MaxIterationsReached``) в композицию через
      :meth:`Specification.or_`).

    Когда использовать:
    - агентский цикл «пока не получим финальный ответ»;
    - повторные попытки/опросы внешнего ресурса до появления
      нужного состояния.

    Когда НЕ использовать:
    - однократный проход по источнику — используйте источник как есть,
      Loop здесь избыточен;
    - stop-условие зависит не от события, а от внешнего таймера —
      нужна другая семантика.
    """

    def __init__(
        self,
        source: StreamSource[TCtx, TOut],
        stop_if: Specification[tuple[TCtx, TOut]],
    ) -> None:
        self._source = source
        self._stop_if = stop_if

    def name(self) -> str:
        return "Loop(" + self._source.name() + ")"

    def stream(self, ctx: TCtx) -> Iterable[TOut]:
        while True:
            for event in self._source.stream(ctx):
                yield event

                if self._stop_if.check((ctx, event)):
                    return


class StreamSourceChainBuilder(Generic[TCtx, TOut]):
    """
    Билдер onion-цепочки :class:`StreamSource`-middleware поверх
    обязательного терминала, по образцу web-фреймворков (ASP.NET Core
    ``UseX().Run()``, Rack ``Rack::Builder``).

    Две стадии API:

    - :meth:`use` регистрирует middleware-factory. Factory получает
      текущий inner ``StreamSource`` и возвращает новый
      ``StreamSource``, который его оборачивает. Порядок регистрации =
      порядок выполнения на «пути запроса»: первое ``use()`` — самый
      внешний слой, ближайший ко входу. Последнее — ближайшее к
      терминалу.

    - :meth:`terminal` принимает терминальный ``StreamSource`` (тот,
      что не делегирует дальше) и собирает финальную цепочку,
      оборачивая терминал факториями в обратном порядке регистрации.
      Терминал — **обязательный** аргумент: забыть его нельзя, без
      него нет цепочки.

    Схема сборки::

                 use(m₁) ─┐        ┌── самый внешний ──┐
                 use(m₂) ─┤        │                   │
                 use(m₃) ─┤        ▼                   │
                    ...   │    ┌─────────┐             │
                 use(mₙ) ─┘    │   m₁    │ ──▶ вход    │
                               └────┬────┘             │
                                    │                  │
                               ┌────▼────┐             │
                               │   m₂    │             │
                               └────┬────┘             │
                                    ▼                  │
                                   ...                 │
                               ┌─────────┐             │
                               │   mₙ    │             │
                               └────┬────┘             │
                                    ▼                  │
                               ┌─────────┐ ◀── самый внутренний
                 terminal() ──▶│terminal │
                               └─────────┘

    Поведение:
    - каждая middleware решает сама, что делать с inner: вызвать
      ``inner.stream(ctx)`` и пересылать события (прозрачная обёртка),
      отфильтровать/обогатить события, пропустить inner целиком
      (short-circuit), обернуть вызов в try/except (retry), замерить
      время и т.п.;
    - ``ctx`` общий по всей цепочке — middleware'ы согласовываются
      через мутации ``ctx`` (``ctx.llm_builder.system_prompt`` и
      т.п.), а не через события.

    Применимость: любые onion-composable :class:`StreamSource`.
    Агентские middleware — лишь один из случаев; тот же паттерн
    уместен для HTTP-клиентов, таск-раннеров, пайплайнов обработки
    сообщений.

    Когда использовать:
    - каждая стадия должна иметь возможность обернуть или заменить
      поведение следующей;
    - нужен явный «терминал», который не делегирует никуда — и
      middleware, оборачивающие его.

    Когда НЕ использовать:
    - стадии независимы и их события нужно просто слить —
      :class:`StreamSourcePipeline` (sequential fan-out);
    - «первый успешный выигрывает» (fallback) — см.
      :class:`ExecutorPipeline` на уровне Executor.
    """

    def __init__(self) -> None:
        self._factories: list[
            Callable[[StreamSource[TCtx, TOut]], StreamSource[TCtx, TOut]]
        ] = []

    def use(
        self,
        factory: Callable[[StreamSource[TCtx, TOut]], StreamSource[TCtx, TOut]],
    ) -> Self:
        """Зарегистрировать middleware-factory.

        Factory — любая callable, принимающая ``inner: StreamSource`` и
        возвращающая обёрнутый ``StreamSource``. Это может быть конструктор
        middleware-класса (``SomeMiddleware``) или лямбда с захваченными
        зависимостями (``lambda inner: SomeMiddleware(inner, dep1, dep2)``).
        """
        self._factories.append(factory)
        return self

    def terminal(
        self, terminal: StreamSource[TCtx, TOut]
    ) -> StreamSource[TCtx, TOut]:
        """Собрать цепочку, обернув ``terminal`` зарегистрированными
        middleware в обратном порядке. Возвращает внешний ``StreamSource``,
        готовый к использованию."""
        chain = terminal
        for factory in reversed(self._factories):
            chain = factory(chain)
        return chain


class ExecutorPipeline(Executor[TCtx, TIn, TOut]):
    """
    Упорядоченный fallback-chain поверх списка :class:`Executor`.

    Хэндлеры пробуются по очереди (в порядке передачи). Первый, отработавший
    без ошибки — выигрывает, его результат возвращается. Если исключение
    удовлетворяет спецификации ``fallback_on`` — переходим к следующему
    хэндлеру. Если не удовлетворяет — исключение летит наружу немедленно,
    без попытки следующего. Если все хэндлеры упали с подходящими ошибками —
    пробрасывается последнее исключение.

    Схема::

                       ctx, req
                          │
                          ▼
                   ┌──────────────┐    success      ┌──────┐
                   │  executor₁   │ ──────────────▶ │ out  │
                   └──────┬───────┘                 └──────┘
                          │ error
                          ▼
                  ┌────────────────┐    нет         ┌────────┐
                  │  fallback_on   │ ─────────────▶ │ raise  │
                  │   .check(e)    │                └────────┘
                  └───────┬────────┘
                          │ да
                          ▼
                   ┌──────────────┐    success      ┌──────┐
                   │  executor₂   │ ──────────────▶ │ out  │
                   └──────┬───────┘                 └──────┘
                          │ error
                          ▼
                         ...
                          │
                          ▼
                   ┌──────────────┐    success     ┌──────┐
                   │  executorₙ   │ ─────────────▶ │ out  │
                   └──────┬───────┘                └──────┘
                          │ error
                          ▼
                    raise last_exc

    Применимо везде, где нужно попробовать несколько источников и взять
    первый успешный (resolver-chain, cache→db, и т.п.).

    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) никогда не
    перехватывается — прерывает цепочку сразу, независимо от ``fallback_on``.

    Примеры ``fallback_on``:
    - ``IsInstance(TimeoutError, ConnectionError)`` — fallback на сетевых;
    - ``IsInstance(HTTPError).and_(HasRetryableStatus())`` — fallback только
      на ретраебельные HTTP-коды.

    Если список ``executors`` пуст, :meth:`execute` поднимает
    :class:`ChainNoHandlersError`.
    """

    def __init__(
        self,
        executors: Sequence[Executor[TCtx, TIn, TOut]],
        fallback_on: ExceptionSpecification,
    ) -> None:
        self._executors = executors
        self._fallback_on = fallback_on

    def execute(self, ctx: TCtx, req: TIn) -> TOut:
        last_exc: Exception | None = None

        for h in self._executors:
            try:
                return h.execute(ctx, req)
            except Exception as e:
                if not self._fallback_on.check(e):
                    raise
                last_exc = e
                continue

        if last_exc is None:
            raise ChainNoHandlersError("Chain: no handlers were tried")

        raise last_exc


class ExecutorConditional(Executor[TCtx, TIn, TOut]):
    """
    Бинарная развилка :class:`Executor` по :class:`Specification`.

    Предикат ``when`` смотрит на пару ``(ctx, req)``. Если он удовлетворён —
    запрос уходит в ``if_true``, иначе — в ``if_false``. Обе ветки должны
    иметь одинаковую сигнатуру ``Executor[TCtx, TIn, TOut]``, так что
    снаружи композиция выглядит как обычный Executor того же типа.

    Схема::

                         ctx, req
                            │
                            ▼
                  ┌───────────────────┐
                  │  when.check       │
                  │   ((ctx, req))    │
                  └────┬──────────┬───┘
                    да │          │ нет
                       ▼          ▼
                 ┌──────────┐ ┌──────────┐
                 │ if_true  │ │ if_false │
                 └────┬─────┘ └────┬─────┘
                      │            │
                      └──────┬─────┘
                             ▼
                            out

    Применимо для простых ветвлений: admin/user, cached/fresh, bulk/single
    и т.п. Для N-арного маршрутинга по ключу — см.
    :class:`ExecutorDispatcher`.
    """

    def __init__(
        self,
        when: Specification[tuple[TCtx, TIn]],
        if_true: Executor[TCtx, TIn, TOut],
        if_false: Executor[TCtx, TIn, TOut],
    ) -> None:
        self._when = when
        self._if_true = if_true
        self._if_false = if_false

    def execute(self, ctx: TCtx, req: TIn) -> TOut:
        if self._when.check((ctx, req)):
            return self._if_true.execute(ctx, req)
        return self._if_false.execute(ctx, req)


class ExecutorRouteError(Exception):
    """В :class:`ExecutorDispatcher` нет handler'а для вычисленного ключа."""


class ExecutorDispatcher(Executor[TCtx, TIn, TOut]):
    """
    Маршрутизация запроса по вычисляемому ключу.

    На каждый вызов из пары ``(ctx, req)`` извлекается ключ через ``key_fn``,
    затем по таблице ``routes`` находится целевой Executor и ему передаётся
    исходный запрос. Если ключ не зарегистрирован — бросается
    :class:`ExecutorRouteError`.

    Схема::

                         ctx, req
                            │
                            ▼
                    ┌──────────────┐
                    │    key_fn    │──▶ key
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐      нет     ┌────────────────────┐
                    │ routes[key]  │ ───────────▶ │ ExecutorRouteError │
                    └──────┬───────┘              └────────────────────┘
                           │ есть
                           ▼
                    ┌──────────────┐
                    │   handler    │──▶ out
                    └──────────────┘

    В отличие от :class:`ExecutorPipeline`, здесь нет fallback-цепочки:
    выбирается один handler, и если он бросает — исключение уходит наружу
    без попытки других маршрутов. Если поверх нужен fallback — оберни
    handler в :class:`ExecutorPipeline` на уровне соответствующего ключа.

    ``routes`` — read-only :class:`Mapping`, ExecutorDispatcher не управляет
    жизненным циклом таблицы. Если нужна динамическая регистрация на
    лету — передайте обычный ``dict`` и мутируйте его снаружи.
    """

    def __init__(
        self,
        routes: Mapping[TKey, Executor[TCtx, TIn, TOut]],
        key_fn: Callable[[TCtx, TIn], TKey],
    ) -> None:
        self._routes = routes
        self._key_fn = key_fn

    def execute(self, ctx: TCtx, req: TIn) -> TOut:
        key = self._key_fn(ctx, req)
        handler = self._routes.get(key)
        if handler is None:
            raise ExecutorRouteError(f"no route for key: {key!r}")
        return handler.execute(ctx, req)
