# boba-agent: ход модели с инструментами проекта для любого подпроекта

Упакованный агент: модель, системный промпт, хранилище истории и реестр инструментов
собираются один раз под субъекта, дальше `ask(context, thread, question)` ведёт один ход,
в котором модель зовёт инструменты, пока ей это нужно, и возвращает итог вместе с
протоколом хода. Цикл сам по себе это `create_agent` из langchain, тот же, что крутит
ход в чате: параллельные вызовы инструментов, проверка их аргументов и сообщения для
окна контекста берутся оттуда.

```
graph.py   GraphSpec, AgentGraphBuilder, PlainGraphBuilder: сборка графа хода
agent.py   Agent, AgentSpec, AgentLimits, AgentReply, AgentError
```

## Инструменты через общий конвейер

Агент получает готовые `BaseTool` и сам их не оборачивает. Откуда они берутся, решает
вызывающий, и путей два, оба через один и тот же конвейер `ToolPipeline` из
`boba-runtime`:

- **чат** берёт инструменты из своего реестра `ToolRegistry`, где к общим слоям добавлены
  права по ролям и профилю, контекст вызова, соединения и билеты пользователя;
- **сервис без пользователя** (индексатор, описатель, планировщик) собирает их
  `ToolSet`'ом: явный список функций с `@tool`, значения их `Injected`-параметров
  готовыми моделями и запускатель.

Слои конвейера у обоих одни: тело исполняется запускателем из `[tool_launcher]`,
процессом или песочницей с образом плагина, вызов получает id и намерение, ведёт журнал,
подчиняется отмене, а исключение тела становится результатом со статусом error. Голую
функцию, минуя конвейер, агенту отдать негде. Инструмент с параметром про пользователя,
`Subject`, `Scope`, корень workspace или соединение субъекта, в `ToolSet` не проходит:
ему нужен реестр и контекст вызова.

## Как пользоваться

```python
from boba.agent import Agent, AgentLimits, GraphSpec, PlainGraphBuilder
from boba.runtime.launchers import ToolLaunchers
from boba.runtime.plugins import ToolSet
from boba.toolkit.manifest import LaunchSpec

launcher = ToolLaunchers.of(raw).launcher_of(
    LaunchSpec(section="ix", modules=("boba.tool.ix.tools",))
)
tools = ToolSet(launcher, [cfg.tools]).build([ix_node_neighbors, ix_node_aspects])

spec = GraphSpec(
    chat=ProviderChatModel(provider=provider, sampling=sampling, model_name=model),
    tools=tools,
    system_prompt=prompt.system_prompt,
    checkpointer=InMemorySaver(),
)
agent = Agent(spec, AgentLimits(max_model_calls=8), PlainGraphBuilder())

reply = await agent.ask("node-42", "Опиши объект по его структуре и соседям.")
reply.text         # итоговый ответ модели
reply.messages     # протокол хода: вопрос, ответы с вызовами, результаты инструментов
reply.model_calls  # сколько раз ходили в модель
reply.tool_calls   # сколько инструментов вызвано
await agent.forget("node-42")
```

Сервису для этого нужна одна секция конфига, `[tool_launcher]`: в `process` это рабочий
каталог и таймауты, в `sandbox` ещё `[env]` с образом плагина. Ролей, профилей и
`conf/plugins` он не заводит. Контекст вызова агенту не нужен; тот, кто работает от
имени пользователя, открывает его снаружи, как делает чат.

Модель это `BaseChatModel` langchain; в проекте это `ProviderChatModel` из `boba-llm`
поверх любого бэкенда: openai-совместимый endpoint, ollama, локальная onnx-модель.

## История

История живёт в checkpointer по thread id: `InMemorySaver` для пакетных задач вроде
описателя, `AsyncPostgresSaver` для чата. Вопросы в одном thread видят предыдущие,
ответ хода отдаёт только сообщения этого хода. `forget(thread)` стирает thread из
хранилища, поэтому пакетная задача может держать память процесса пустой: thread на
объект, после записи итога thread стирается.

## Пределы и ошибки

`max_model_calls` ограничивает обращения к модели за один вопрос слоем перед моделью;
исчерпание это `AgentError`, а не тихий обрыв. Предел рекурсии графа считается от числа
слоёв так, чтобы первым срабатывал предел обращений. Ошибка инструмента в ход не
прорывается: конвейер отдаёт её модели результатом со статусом error, и модель может
исправиться. Наружу уходят только отказ модели, предел и переполнение графа, всё как
`AgentError`.

## Кто пользуется

Чат: `GraphSpec` и построители графа переехали сюда из `boba-chainlit`, чат подставляет
свой `PrefetchGraphBuilder`, postgres-checkpointer и слой представления истории через
`middleware`. Описатель ix берёт `Agent` целиком с `InMemorySaver`.

## Тесты

Быстрые идут на модели по сценарию и фейковом инструменте стенда, собранном `ToolSet`'ом
под субпроцесс хоста. Интеграционный (`-m integration`) собирает инструмент `bash`
запускателем из `[tool_launcher]` приложения и проверяет, что локальная onnx-модель из
секции `[agent_stand]` файла `stand.toml` (`model_dir`, `max_tokens`) выполняет им команду.
