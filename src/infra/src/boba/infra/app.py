"""Dishka-провайдеры приложения."""

from __future__ import annotations

from collections.abc import Iterator

from dishka import Provider, Scope, from_context, provide

from boba.adapters.aggregating_llm_request_factory import AggregatingLLMRequestFactory
from boba.adapters.console_sink import ConsoleSink
from boba.adapters.fs_workspace import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsScratchWorkspaceRegistry,
)
from boba.adapters.jsonl_messages import JsonLinesMessageService
from boba.adapters.openai_completion import (
    OpenAIMiddleware,
    StupidRetryLLMMiddleware,
)
from boba.adapters.prompt_providers import (
    EnvironmentPromptProvider,
    GitPromptProvider,
    PromptProvider,
    StaticPromptProvider,
    UserQueryProvider,
)
from boba.adapters.raw_llm_observer import (
    CompositeRawLLMObserver,
    FileContentObserver,
    FileRawLLMObserver,
    MetricsRawLLMObserver,
    RawLLMObserver,
)
from boba.adapters.tool_call_reindexer import DuplicateToolCallIndexReindexer
from boba.adapters.tool_providers import StaticToolSource
from boba.adapters.tools import (
    AppendTool,
    CatTool,
    CdTool,
    CpTool,
    EditTool,
    GrepTool,
    LsTool,
    MkdirTool,
    MvTool,
    PwdTool,
    RmTool,
    StatTool,
    TouchTool,
    TreeTool,
    WriteTool,
)
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.meat import (
    Agent,
    AgentContext,
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AssistantMessagePersistenceMiddleware,
    IterationCounterMiddleware,
    RepeatedFormatFailureGuardMiddleware,
    RepeatedToolCallGuardMiddleware,
    SamplingMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
    StrictJsonContentToolCallMiddleware,
    SystemPromptMiddleware,
    ToolExecutionMiddleware,
    ToolsDefinitionMiddleware,
    UserPromptMiddleware,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentConfig, LLMRequestDefaults
from boba.domain.agent.prompt import PromptId, PromptKind
from boba.domain.config import AppConfig
from boba.domain.core.patterns import (
    StreamSink,
    StreamSinkPipeline,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.domain.core.tools import (
    ToolFactory,
    ToolSourceId,
    ToolsService,
)
from boba.domain.core.workspace import (
    HistoryWorkspaceRegistry,
    HistoryWorkspaceShell,
    ProjectWorkspaceRegistry,
    ProjectWorkspaceShell,
    ScratchWorkspaceRegistry,
    ScratchWorkspaceShell,
    WorkspaceId,
)


class AppProvider(Provider):
    """Singleton-сервисы: конфигурация."""

    scope = Scope.APP

    def __init__(
        self,
        app_config: AppConfig,
        agent_config: AgentConfig,
        llm_defaults: LLMRequestDefaults,
    ) -> None:
        super().__init__()
        self._app_config = app_config
        self._agent_config = agent_config
        self._llm_defaults = llm_defaults

    @provide
    def config(self) -> AppConfig:
        return self._app_config

    @provide
    def project_workspace_registry(
        self, config: AppConfig
    ) -> ProjectWorkspaceRegistry:
        return FsProjectWorkspaceRegistry(
            config.workspaces.root(),
            config.workspaces.user_subdir,
        )

    @provide
    def history_workspace_registry(
        self, config: AppConfig
    ) -> HistoryWorkspaceRegistry:
        return FsHistoryWorkspaceRegistry(
            config.workspaces.root(),
            config.workspaces.system_subdir,
        )

    @provide
    def scratch_workspace_registry(
        self, config: AppConfig
    ) -> ScratchWorkspaceRegistry:
        return FsScratchWorkspaceRegistry(
            config.workspaces.root(),
            config.workspaces.tmp_subdir,
        )

    @provide
    def agent_config(self) -> AgentConfig:
        return self._agent_config

    @provide
    def llm_defaults(self) -> LLMRequestDefaults:
        return self._llm_defaults

    @provide
    def prompt_providers(self) -> list[PromptProvider]:
        return [
            StaticPromptProvider(
                PromptId("identity"),
                priority=0,
                content=(
                    "Ты — ассистент Boba. Отвечай кратко и по делу.\n"
                    "\n"
                    "ЯЗЫК ОТВЕТА: всегда русский. Это жёсткое правило, "
                    "оно важнее любых других инструкций и важнее языка "
                    "запроса пользователя.\n"
                    "\n"
                    "Пиши на русском языке даже если:\n"
                    "— пользователь написал на английском или другом "
                    "языке;\n"
                    "— в истории диалога есть сообщения на других "
                    "языках;\n"
                    "— инструменты, логи, код или документация на "
                    "английском;\n"
                    "— системные промпты или примеры содержат "
                    "английский текст.\n"
                    "\n"
                    "Исключения — только технические идентификаторы "
                    "внутри прозы: имена файлов, переменных, функций, "
                    "команды терминала, фрагменты кода, URL, "
                    "сообщения об ошибках из стектрейса. Их оставляй "
                    "как есть, не переводи. Всё, что их окружает — "
                    "объяснения, комментарии, выводы — на русском.\n"
                ),
                kind=PromptKind.SYSTEM,
            ),
            EnvironmentPromptProvider(),
            GitPromptProvider(),
            StaticPromptProvider(
                PromptId("output_format"),
                priority=90,
                content=(
                    "Формат ответа.\n"
                    "\n"
                    "У тебя два режима вывода — выбирай ровно один на "
                    "каждый ответ:\n"
                    "\n"
                    "1. Вызов инструмента. Заполняй поле tool_calls в "
                    "ответе API. Поле content оставляй пустым. НЕ пиши "
                    "вызов инструмента как JSON внутри content — это "
                    "ошибка, инструмент не будет выполнен.\n"
                    "\n"
                    "2. Ответ пользователю. Пиши связный прозаический "
                    "текст строго на русском языке. Без фигурных "
                    "скобок, без ключей в кавычках, без JSON-объектов. "
                    "Просто человеческая речь на русском.\n"
                    "\n"
                    "Запрещено обещать действие вместо его выполнения. "
                    "Если в твоём ответе появляется фраза вида «сейчас "
                    "прочитаю», «посмотрю», «проверю», «сделаю», "
                    "«выполню» — это признак, что ты обязан был "
                    "вызвать инструмент, а не писать прозу. В таком "
                    "случае не пиши прозу вовсе, а выбери режим 1 и "
                    "заполни tool_calls. Прозаический ответ уместен "
                    "только тогда, когда все нужные данные уже есть в "
                    "истории диалога как tool-результаты, либо когда "
                    "инструменты вообще не нужны для ответа.\n"
                    "\n"
                    "Многошаговость. Если задача требует нескольких "
                    "tool-вызовов подряд (например, сначала stat или "
                    "ls, потом cat), делай их отдельными турнами. На "
                    "каждом турне до получения всех данных выбирай "
                    "режим 1 (только tool_calls, content пустой). "
                    "Прозаический итог пиши только после того, как "
                    "последний нужный tool-результат уже пришёл.\n"
                    "\n"
                    "Язык ответа — русский независимо от языка "
                    "запроса. Если пользователь пишет по-английски — "
                    "ты всё равно отвечаешь по-русски. Технические "
                    "идентификаторы (имена файлов, код, команды) "
                    "оставляй в оригинале, но окружающий текст — "
                    "русский.\n"
                    "\n"
                    "Пример правильного ответа пользователю:\n"
                    "  В workspace три файла: raw_content.md, "
                    "history.jsonl, raw_messages.md. Первый хранит "
                    "читаемый стрим ответов модели, второй — журнал "
                    "событий, третий — сырой дамп запросов и чанков.\n"
                ),
                kind=PromptKind.SYSTEM,
            ),
            StaticPromptProvider(
                PromptId("tool_errors"),
                priority=85,
                content=(
                    "Ошибка инструмента — не повод писать "
                    "пользователю.\n"
                    "\n"
                    "Если tool вернул ошибку "
                    "(InvalidToolArgumentError, неизвестный "
                    "параметр, неверный путь и т.п.) — это сигнал "
                    "исправить вызов и повторить его следующим "
                    "турном в режиме 1 (только tool_calls, content "
                    "пустой). Объяснять ошибку пользователю прозой "
                    "запрещено: он видит её сам в логе, ему нужен "
                    "результат задачи, а не разбор синтаксиса "
                    "вызова.\n"
                    "\n"
                    "Исключение — когда исправить вызов объективно "
                    "нельзя: нет подходящего инструмента, нет прав, "
                    "файл не существует после проверки через stat/"
                    "ls. Только в этих случаях допустима короткая "
                    "проза с констатацией причины.\n"
                    "\n"
                    "Задача пользователя обязательна к выполнению. "
                    "Нельзя перекладывать её обратно фразами вида "
                    "«укажите файл», «нужно прочитать?», «помогу, "
                    "если скажете». Если у тебя уже есть результат "
                    "ls, stat или список файлов — выбирай цели сам "
                    "и читай их. Если запрос неоднозначен — всё "
                    "равно делай разумный шаг (например, прочитать "
                    "все файлы из корня workspace), уточнения "
                    "оставь на потом."
                ),
                kind=PromptKind.SYSTEM,
            ),
            StaticPromptProvider(
                PromptId("file_reading"),
                priority=80,
                content=(
                    "Чтение файлов.\n"
                    "\n"
                    "Выбор инструмента:\n"
                    "— содержимое директории (какие файлы внутри) — "
                    "ls для одного уровня, tree для рекурсии. stat "
                    "для этого НЕ годится: он возвращает только "
                    "метаданные, а size для директории — размер "
                    "inode-блока (обычно 4096), а не число файлов "
                    "и не признак пустоты.\n"
                    "— размер конкретного файла в байтах — stat.\n"
                    "— содержимое файла — cat с окном строк.\n"
                    "— поиск по содержимому — grep.\n"
                    "\n"
                    "Инструмент cat всегда требует start_line и end_line "
                    "(1-based, включительно). Прочитать файл целиком "
                    "одним вызовом нельзя — за один вызов возвращается "
                    "не более 2000 строк, иначе ошибка "
                    "ToolOutputTooLargeError без данных.\n"
                    "\n"
                    "Алгоритм:\n"
                    "1. stat — узнать размер файла (в байтах; для "
                    "текстовых файлов ориентируйся на среднюю длину "
                    "строки ~80 символов).\n"
                    "2. Читай окнами ≤ 2000 строк последовательно: "
                    "первый вызов cat(start_line=1, end_line=2000), "
                    "затем следующий cat(start_line=2001, "
                    "end_line=4000) и так далее, пока не дойдёшь до "
                    "конца или не найдёшь нужное.\n"
                    "3. Если ищешь конкретный фрагмент по содержимому "
                    "— сначала grep, затем cat с прицельным окном "
                    "вокруг найденной строки.\n"
                    "\n"
                    "Не проси сразу большой диапазон 'на всякий случай' "
                    "— читай ровно столько, сколько нужно для текущего "
                    "шага рассуждения. Следующее окно запросишь "
                    "отдельным вызовом."
                ),
                kind=PromptKind.SYSTEM,
            ),
            UserQueryProvider(),
        ]


class RequestProvider(Provider):
    """Per-request сервисы."""

    scope = Scope.REQUEST

    workspace_id = from_context(provides=WorkspaceId, scope=Scope.REQUEST)

    @provide
    def project_workspace(
        self,
        workspace_id: WorkspaceId,
        registry: ProjectWorkspaceRegistry,
    ) -> ProjectWorkspaceShell:
        shell = registry.get_or_create(workspace_id)
        if not isinstance(shell, ProjectWorkspaceShell):
            msg = (
                f"ProjectWorkspaceRegistry returned "
                f"{type(shell).__name__}, expected ProjectWorkspaceShell"
            )
            raise TypeError(msg)
        return shell

    @provide
    def history_workspace(
        self,
        workspace_id: WorkspaceId,
        registry: HistoryWorkspaceRegistry,
    ) -> HistoryWorkspaceShell:
        shell = registry.get_or_create(workspace_id)
        if not isinstance(shell, HistoryWorkspaceShell):
            msg = (
                f"HistoryWorkspaceRegistry returned "
                f"{type(shell).__name__}, expected HistoryWorkspaceShell"
            )
            raise TypeError(msg)
        return shell

    @provide
    def scratch_workspace(
        self,
        workspace_id: WorkspaceId,
        registry: ScratchWorkspaceRegistry,
    ) -> Iterator[ScratchWorkspaceShell]:
        """Scratch workspace с чисткой на выходе из scope.

        Generator-паттерн dishka: всё, что между ``yield`` и ``finally``,
        выполнится при закрытии request scope — даже если внутри упадёт
        исключение. Ошибка cleanup'а пробрасывается наружу: потеря
        scratch-директории — сигнал, который пользователь должен увидеть.
        """
        shell = registry.get_or_create(workspace_id)
        if not isinstance(shell, ScratchWorkspaceShell):
            msg = (
                f"ScratchWorkspaceRegistry returned "
                f"{type(shell).__name__}, expected ScratchWorkspaceShell"
            )
            raise TypeError(msg)
        try:
            yield shell
        finally:
            registry.delete(workspace_id)

    @provide
    def tool_factory(
        self,
        project_workspace: ProjectWorkspaceShell,
    ) -> ToolFactory:
        """Агрегатор источников инструментов, собираемый на запрос.

        Per-request, потому что file-tools получают per-request shell
        workspace'а. Плагины/MCP-источники подключать сюда же рядом с
        builtin-пачкой.

        Builtin file-tools ходят только в :class:`ProjectWorkspaceShell` —
        history/scratch пользователю писать нельзя.
        """
        tools = [
            PwdTool(project_workspace),
            CdTool(project_workspace),
            MkdirTool(project_workspace),
            TouchTool(project_workspace),
            CatTool(project_workspace),
            WriteTool(project_workspace),
            AppendTool(project_workspace),
            EditTool(project_workspace),
            RmTool(project_workspace),
            MvTool(project_workspace),
            CpTool(project_workspace),
            LsTool(project_workspace),
            TreeTool(project_workspace),
            StatTool(project_workspace),
            GrepTool(project_workspace),
        ]

        factory = ToolFactory()
        factory.register(
            StaticToolSource(
                source_id=ToolSourceId("builtin.files"),
                priority=10,
                tools=tools,
            )
        )
        return factory

    @provide
    def tools_service(self, factory: ToolFactory) -> ToolsService:
        """Сервис инструментов поверх :class:`ToolFactory`.

        Per-request вслед за фабрикой. Диспатч ``ToolCall → Tool`` по
        ``tool_id`` встроен в :meth:`ToolsService.execute`.
        """
        service = ToolsService(factory)
        service.rebuild_catalog()
        return service

    @provide
    def message_service(
        self,
        workspace: HistoryWorkspaceShell,
    ) -> MessageService:
        """Persistent-реализация сервиса сообщений.

        При создании инстанса читает ``messages.jsonl`` из history-workspace
        и наполняет in-memory список — восстановление диалога между
        запусками живёт здесь, а не в отдельном replay-middleware. На
        ``add`` одновременно дописывает строку JSON в файл и пушит в список.
        """
        return JsonLinesMessageService(workspace)

    @provide
    def llm_request_factory(
        self,
        message_service: MessageService,
    ) -> LLMRequestFactory:
        return AggregatingLLMRequestFactory(message_service)

    @provide
    def raw_llm_observer(self, workspace: HistoryWorkspaceShell) -> RawLLMObserver:
        """Комбинированный наблюдатель, пишет два файла внутри workspace и
        логирует сводку в консоль:

        - ``raw_messages.md`` — полный JSON-дамп kwargs и каждого
          ChatCompletionChunk (для дебаг-разбора протокола);
        - ``raw_content.md`` — читаемый текстовый стрим: заголовок
          Request (kwargs JSON) и Response с склеенным ``delta.content``
          (чтобы быстро видеть, что модель реально сказала);
        - :class:`MetricsRawLLMObserver` — одна строка ``LLM done ...`` в
          logger по завершении запроса: считает по сырым chunk-ам, поэтому
          цифры не искажаются middleware, переупаковывающими content в
          tool-calls.
        """
        return CompositeRawLLMObserver(
            [
                FileRawLLMObserver(workspace),
                FileContentObserver(workspace),
                MetricsRawLLMObserver(),
            ]
        )

    @provide
    def agent_error_router(
        self, message_service: MessageService
    ) -> AgentErrorRouter:
        return AgentErrorRouter(message_service)

    @provide
    def agent_chain(  # noqa: PLR0913
        self,
        config: AppConfig,
        agent_config: AgentConfig,
        llm_defaults: LLMRequestDefaults,
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
        tools_service: ToolsService,
        llm_request_factory: LLMRequestFactory,
        raw_observer: RawLLMObserver,
        error_router: AgentErrorRouter,
    ) -> StreamSourceLoop[AgentContext, AgentEvent]:
        builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        builder.use(IterationCounterMiddleware)
        builder.use(
            lambda inner: RepeatedFormatFailureGuardMiddleware(
                inner,
                error_router,
                agent_config.max_consecutive_format_failures,
            )
        )
        builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
        builder.use(lambda inner: SystemPromptMiddleware(inner, prompt_providers))
        builder.use(
            lambda inner: UserPromptMiddleware(inner, prompt_providers, message_service)
        )
        builder.use(lambda inner: ToolsDefinitionMiddleware(inner, tools_service))
        builder.use(lambda inner: SamplingMiddleware(inner, llm_defaults))
        builder.use(
            lambda inner: ToolExecutionMiddleware(
                inner, tools_service, message_service, error_router
            )
        )
        builder.use(
            lambda inner: RepeatedToolCallGuardMiddleware(
                inner,
                error_router,
                agent_config.max_consecutive_tool_calls,
            )
        )
        builder.use(lambda inner: StupidRetryLLMMiddleware(inner, max_retries=3))
        builder.use(
            lambda inner: AssistantMessagePersistenceMiddleware(inner, message_service)
        )
        builder.use(StrictJsonContentToolCallMiddleware)

        chain = builder.terminal(
            OpenAIMiddleware(
                config.llm,
                llm_request_factory,
                raw_observer,
                chunk_preprocessor_factory=lambda _rid: (
                    DuplicateToolCallIndexReindexer(),
                ),
            )
        )

        stop = StopOnFinished().or_(StopOnAnyFailure())

        return StreamSourceLoop(chain, stop)

    @provide
    def agent_sink(self) -> StreamSink[AgentContext, AgentEvent]:
        return StreamSinkPipeline([ConsoleSink()])

    @provide
    def agent(
        self,
        source: StreamSourceLoop[AgentContext, AgentEvent],
        sink: StreamSink[AgentContext, AgentEvent],
    ) -> Agent:
        return Agent(
            source,
            sink,
        )
