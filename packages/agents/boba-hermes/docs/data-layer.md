# HermesDataLayer

План замены `PostgresDataLayer` на гибридный слой: история чатов живёт в hermes
(`state.db` профиля), а то, чего в его API нет, остаётся в postgres.

## Границы

| Сущность | Где | Почему |
|---|---|---|
| Тред (сессия) | hermes, `GET/POST/PATCH/DELETE /api/sessions` | единый источник истории |
| Сообщения | hermes, `GET /api/sessions/{id}/messages` | пишет сам агент во время хода |
| Пользователь | postgres, `chainlit.users` | у hermes нет понятия chainlit-пользователя |
| Связка пользователь → профиль | postgres, `chainlit.hermes_profiles` | имя профиля не выводится из логина |
| Feedback (👍/👎) | postgres, `chainlit.feedbacks` | в API hermes отсутствует |
| Элементы (вложения) | postgres `chainlit.elements` + `LocalStorageClient` | в API hermes отсутствует |
| Избранные шаги | postgres, `chainlit.steps` (`meta.favorite`) | в API hermes отсутствует |
| Владелец треда | postgres, `chainlit.threads` (`id → user_id`) | в http-роутах chainlit нет контекста пользователя |

Таблица `steps` после перехода не используется: сообщения переезжают в hermes.
А `threads` остаётся — но как индекс владельца, а не хранилище истории: в ней
живёт связка `thread_id → user_id`. Без неё нельзя определить профиль в
`get_thread`/`delete_thread`: chainlit зовёт их только с `thread_id`, а
контекста пользователя в http-роутах нет. Владелец узнаётся один раз, когда
сервер сам вызывает `update_thread(thread_id, user_id=...)` при создании треда.

Внешних ключей в схеме нет, поэтому `elements` и `feedbacks` спокойно ссылаются
на id, пришедшие из hermes.

Файловый доступ к данным hermes исключён: chainlit не монтирует его том и
общается с ним только по HTTP.

## Ключевые решения

**`thread_id` chainlit = `session_id` hermes.** `POST /api/sessions` принимает `id`
от клиента, поэтому маппинг не нужен: chainlit генерирует uuid, мы заводим сессию
с этим же id. Валидация hermes: без `\r\n\0`, не path-unsafe, не длиннее 256
символов — uuid проходит.

**Профиль = пользователь.** Все запросы идут на `/p/<профиль>/…`, где профиль
получен из логина (`HermesProfileName`). Значит `list_threads` не нуждается в
фильтре по пользователю: в профиле лежат только его сессии.

**Заголовок треда — best-effort.** У hermes `title` уникален в пределах профиля,
потому что служит адресуемым именем: `resolve_session_by_title` находит по нему
сессию для слэш-команд `/resume`, `/title`, `/history`, `/branch`. У chainlit имя
треда — просто подпись в сайдбаре, выведенная из первого сообщения, и повторы там
обычны. Подстраиваемся мы: при 400 `invalid_title` повторяем `PATCH` с именем
`<имя> #N`. Формат не произвольный — это родная схема hermes: он сам так нумерует
ветки и продолжения (`get_next_title_in_lineage`), а `resolve_session_by_title`
считает `#N` вариантами одного имени и по `/resume <имя>` отдаёт самый свежий.
Суффикс из `thread_id` такого эффекта не даст.

В `ThreadDict.name` попадает то, что реально записалось, — сайдбар chainlit и
`/resume` в hermes показывают одно и то же. Заголовок освобождается при удалении
сессии, поэтому пересоздание треда с тем же именем работает.

**Профиль заводится через API, файловые системы разделены.** chainlit не
монтирует том hermes и ничего в нём не создаёт: единственный канал — HTTP на
api_server. Своего эндпоинта для профилей у api_server сейчас нет, он добавляется
в форк отдельным шагом (`POST /api/profiles` под тем же `API_SERVER_KEY`, внутри
родной `create_profile` с клоном конфига из донора). До этого профили заводятся
вручную (`hermes profile create`), слой их только использует.

**Связка живёт в таблице, а не в имени.** `chainlit.hermes_profiles`
(`user_id PK`, `profile UNIQUE`, `created_at`) — имя профиля закрепляется за
пользователем при первом входе и больше не пересчитывается. Отсюда главное: смена
логина в AD не уводит пользователя в чужую историю. Обратное направление
(профиль → пользователь) — обычный `SELECT`, а не расшифровка имени. Код:
`HermesProfileRepository` в `chat/data/profiles.py`.

**Имя профиля — читаемое, с запасным вариантом.** `HermesProfileName.encode`
переводит логин в алфавит hermes (`-XX` для всего, что вне `[a-z0-9_]`), чтобы
профиль был узнаваем в `hermes profile list` и TUI. Домен снимает провайдер
авторизации, поэтому kerberos, ldap и local дают один профиль. Если логин не
переводится (кириллица, длина, первый символ не `[a-z0-9]`) или имя занято другим
пользователем — берётся `users.id`: uuid сам по себе валиден как имя профиля
(36 символов, только hex и дефисы).

**409 и 404 на повторе — это успех.** `POST /api/sessions` и `fork` отвечают 409
`session_exists`, `DELETE` — 404 `session_not_found`. Оба кода означают, что
состояние уже нужное, и слой не выносит их наружу как ошибку.

**`create_step`/`update_step` ничего не пишут.** Сообщения в историю добавляет сам
hermes, когда исполняет ход через `POST /api/sessions/{id}/chat/stream`. Если
дублировать их из chainlit, история раздвоится. Методы остаются no-op (кроме
`meta.favorite` в postgres).

**Ход агента идёт через сессию, а не через `/v1/runs`.** Проверено на живом
api_server: `/v1/runs` историю сессии не читает — второй ход в той же сессии
приходит к модели без контекста, историю пришлось бы каждый раз слать в
`conversation_history`. `POST /api/sessions/{id}/chat/stream` берёт историю из
самой сессии (`_conversation_history_for_session`) и туда же дописывает ход,
поэтому chainlit шлёт только текст сообщения.

## Подключение к postgres

Kerberos, без пароля: доменная учётка `boba-svc`, она же владелец базы `boba`.

| Параметр | Значение |
|---|---|
| host / dbname / user | `postgres-17` / `boba` / `boba-svc` |
| gssencmode | `prefer` |
| схема | `chainlit` |
| keytab | `build/local/keytab/boba-svc.keytab` → `/etc/boba-svc.keytab` |

`KRB5_CLIENT_KTNAME` заставляет GSSAPI получить и обновлять тикет из keytab, так
что `kinit` в контейнере не нужен. `KRB5CCNAME=FILE:/tmp/krb5cc` обязателен:
`krb5.conf` контура указывает ccache в keyring, которого в контейнере нет.

Тесты гоняются только внутри сети docker (`make pytest` в `build/`): `pg_hba`
пускает доменные учётки по gss, а с хоста имя `postgres-17` не резолвится.
Изоляция тестов — схема `chainlit_test` в той же базе; отдельную базу не заводим,
для неё сервисной учётке нужен `CREATEDB`.

## Контракт chainlit: `BaseDataLayer`

Полный список абстрактных методов (`chainlit/data/base.py`) и что делает каждый в
`HermesDataLayer`.

| Метод | Сигнатура | Реализация |
|---|---|---|
| `get_user` | `(identifier: str) -> PersistedUser \| None` | postgres, как сейчас |
| `create_user` | `(user: User) -> PersistedUser \| None` | postgres + `HermesProfileRepository.ensure`; заведение профиля в hermes — задача будущего `POST /api/profiles` |
| `get_thread` | `(thread_id: str) -> ThreadDict \| None` | `GET /api/sessions/{id}` + `GET …/messages`, сборка `steps`; `elements`/`feedback` — из postgres |
| `list_threads` | `(pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]` | `GET /api/sessions?limit&offset`, курсор = offset |
| `update_thread` | `(thread_id: str, name=None, user_id=None, metadata=None, tags=None) -> None` | `PATCH /api/sessions/{id}` с `title`; сессия заводится, если её ещё нет |
| `delete_thread` | `(thread_id: str) -> None` | `DELETE /api/sessions/{id}` + чистка `elements`/`feedbacks` |
| `get_thread_author` | `(thread_id: str) -> str` | `hermes_profiles` → `users.identifier`, запрос к hermes не нужен |
| `create_step` | `(step_dict: StepDict) -> None` | no-op: историю пишет hermes |
| `update_step` | `(step_dict: StepDict) -> None` | только `meta.favorite` → postgres |
| `delete_step` | `(step_id: str) -> None` | no-op: у hermes нет удаления отдельного сообщения |
| `get_favorite_steps` | `(user_id: str) -> list[StepDict]` | postgres: id избранных → добор сообщений из hermes |
| `upsert_feedback` | `(feedback: Feedback) -> str` | postgres |
| `delete_feedback` | `(feedback_id: str) -> bool` | postgres |
| `create_element` | `(element: Element) -> None` | `LocalStorageClient` + postgres |
| `get_element` | `(thread_id: str, element_id: str) -> ElementDict \| None` | postgres |
| `delete_element` | `(element_id: str, thread_id: str \| None = None) -> None` | postgres + файл |
| `build_debug_url` | `() -> str` | `""` |
| `close` | `() -> None` | закрыть httpx-клиент и пул |

`set_step_favorite(step_dict, favorite)` не абстрактный — базовая реализация
проставляет `metadata.favorite` и зовёт `update_step`, этого достаточно.

Декоратор `@queue_until_user_message()` на `create_element`, `delete_element`,
`create_step`, `update_step`, `delete_step` остаётся как есть — он в базовом классе.

## API hermes: методы, которые нужны слою

Все пути с префиксом профиля: `/p/<профиль>/…`. Заголовок
`Authorization: Bearer <API_SERVER_KEY>` обязателен, иначе 401.

### `GET /api/sessions`

Список сессий профиля.

| Параметр | Тип | Умолчание | Примечание |
|---|---|---|---|
| `limit` | query int | 50 | максимум 200 |
| `offset` | query int | 0 | максимум 1 000 000 |
| `source` | query str | — | фильтр по источнику сессии |
| `include_children` | query bool | false | ветки, созданные fork |

Ответ: `{"object": "list", "data": [<session>], "limit": int, "offset": int, "has_more": bool}`.
`has_more` вычисляется как `len(data) == limit`.

### `POST /api/sessions`

Создаёт пустую сессию. **Не идемпотентен**: повторный `id` → 409
`session_exists`, поэтому слой либо ловит 409 как «уже есть», либо сперва делает
`GET`.

| Поле body | Тип | Примечание |
|---|---|---|
| `id` / `session_id` | str | если не задан — `api_<ts>_<hex>` |
| `title` | str | должен быть уникален в профиле, иначе 409 |
| `model` | str | по умолчанию `API_SERVER_MODEL_NAME` |
| `system_prompt` | str | снимок промпта сессии |
| `source` | str | нормализуется до `api_server` / `cli` / `telegram` / … |

Ответ: `{"object": "hermes.session", "session": <session>}`.

### `GET /api/sessions/{session_id}`

Ответ: `{"object": "hermes.session", "session": <session>}`; 404 с
`code: session_not_found`, если сессии нет.

### `PATCH /api/sessions/{session_id}`

| Поле body | Тип | Примечание |
|---|---|---|
| `title` | str \| null | null и пробельная строка очищают заголовок |
| `end_reason` | str | закрывает сессию |

Любое другое поле → 400 `unsupported_session_field`. Занятый заголовок →
400 `invalid_title`: `set_session_title` требует уникальности в пределах профиля.

### `DELETE /api/sessions/{session_id}`

Без параметров.

### `GET /api/sessions/{session_id}/messages`

Ответ: `{"object": "list", "session_id": str, "data": [<message>]}`. Перед чтением
id прогоняется через `resolve_resume_session_id`, поэтому для ветки вернётся
история корня.

### `POST /api/sessions/{session_id}/fork`

| Поле body | Тип |
|---|---|
| `id` / `session_id` | str — id новой ветки |
| `title` | str |

Нужен, если появится «продолжить с этого места»; для data layer не обязателен.

## Идемпотентность

Проверено запросами к работающему сервису, не по коду.

| Запрос | 1-й раз | Повтор | Тело ответа |
|---|---|---|---|
| `GET /api/sessions` | 200 | 200 | `{object: list, data, limit, offset, has_more}` |
| `GET /api/sessions/{id}` | 200 | 200 | `{object: hermes.session, session}` |
| `GET /api/sessions/{id}/messages` | 200 | 200 | `{object: list, session_id, data}` |
| `POST /api/sessions` | 201 | **409** `session_exists` | сессия / ошибка |
| `POST /api/sessions/{id}/fork` | 201 | **409** `session_exists` | ветка / ошибка |
| `PATCH` `{title}` | 200 | 200 | сессия; свой же заголовок конфликтом не считается |
| `PATCH` `{title: null}` | 200 | 200 | сессия с пустым заголовком |
| `PATCH` `{end_reason}` | 200 | 200 | сессия; повторное закрытие не ошибка |
| `DELETE /api/sessions/{id}` | 200 | **404** `session_not_found` | `{object: hermes.session.deleted, id, deleted: true}` |

Ошибки на несуществующей сессии — везде 404 `session_not_found`: `GET`, `GET
…/messages`, `PATCH`, `DELETE`.

Прочие проверенные коды:

| Ситуация | Код | Код ошибки |
|---|---|---|
| Заголовок занят другой сессией (`POST` или `PATCH`) | 400 | `invalid_title` |
| Неизвестное поле в `PATCH` | 400 | `unsupported_session_field` |
| Нет/неверный `Authorization` | 401 | `gateway_auth_failed` |
| Профиль в `/p/<профиль>/` не обслуживается | 404 | — |

Итог для слоя: не идемпотентны только `POST /api/sessions` и `fork` (409) и
`DELETE` (404 на повторе). Оба кода означают «состояние уже такое, как надо», и
трактуются слоем как успех — `create` ловит 409, `delete` ловит 404.

**Заголовок освобождается при удалении сессии**: после `DELETE` сессии с
заголовком `X` новую сессию с тем же `X` создать можно (201).

## Модели данных

### `<session>` → `ThreadDict`

Поля сессии (`_session_response`): `id`, `source`, `user_id`, `model`, `title`,
`started_at`, `ended_at`, `end_reason`, `message_count`, `tool_call_count`,
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`reasoning_tokens`, `estimated_cost_usd`, `actual_cost_usd`, `api_call_count`,
`parent_session_id`, `last_active`, `preview`, `has_system_prompt`,
`has_model_config`.

| `ThreadDict` | Источник |
|---|---|
| `id` | `session.id` |
| `createdAt` | `session.started_at` (unix float) → ISO-8601 |
| `name` | `session.title`; если пусто — `session.preview` |
| `userId` | `users.id` владельца профиля |
| `userIdentifier` | `users.identifier` (через `hermes_profiles`) |
| `tags` | `None` |
| `metadata` | счётчики токенов, `model`, `message_count` — для отладки |
| `steps` | из `messages`, см. ниже |
| `elements` | postgres |

### `<message>` → `StepDict`

Поля сообщения (`_message_response`): `id`, `session_id`, `role`, `content`,
`tool_call_id`, `tool_calls`, `tool_name`, `timestamp`, `token_count`,
`finish_reason`, `reasoning`, `reasoning_content`.

| `role` | `StepDict.type` | Поля |
|---|---|---|
| `user` | `user_message` | `output` = `content` |
| `assistant` | `assistant_message` | `output` = `content`; `reasoning` — в `metadata` |
| `tool` | `tool` | `name` = `tool_name`, `output` = `content`, `parentId` = шаг с `tool_call_id` |
| `system` | пропускается | системный промпт в UI не показываем |

Общее: `id` = `message.id`, `threadId` = `session_id`, `createdAt`/`start`/`end`
= `timestamp` → ISO-8601, `feedback` — из `chainlit.feedbacks` по `id`.

## Чего API не даёт

| Требование chainlit | Ограничение hermes | Что делаем |
|---|---|---|
| Имя треда — произвольное | заголовок уникален: по нему работает `/resume` | при 400 `invalid_title` повторяем с `<имя> #N` — родной формат нумерации hermes |
| `ThreadFilter.search` | у `GET /api/sessions` нет поиска | фильтруем `title`/`preview` в пределах страницы; в UI это выглядит как поиск по видимому |
| `ThreadFilter.feedback` | оценок нет | фильтр игнорируем |
| Курсорная пагинация | только `limit`/`offset` | курсор = строковый offset |
| `delete_step` | удаления сообщения нет | no-op |
| `create_user` | пользователей нет | заводим профиль |

## Отклонённые варианты

| Вариант | Почему отклонён |
|---|---|
| История в postgres, hermes stateless | теряются компрессия истории, `/resume`, `fork` и поиск по сессиям — то, ради чего берётся hermes |
| Создание профиля записью каталога на общий том | chainlit пришлось бы монтировать `HERMES_HOME` на запись и выравнивать владельца между контейнерами; песочница hermes должна оставаться закрытой |
| Имя профиля как функция от логина (обратимый кодек) | логин может смениться, а профиль должен остаться; регистр кодеком не восстанавливается, кириллица и длинные логины не кодируются вовсе |
| Хранить связку в `users.meta` | это `metadata` пользователя из chainlit, туда пишут auth-провайдеры — привязку легко потерять при перезаписи |
| Сайдкар-файл под feedback и элементы | postgres уже в схеме и хранит их сейчас; отдельный файловый формат — лишняя сущность |
| Профили через дашборд (`POST /api/profiles`, basic auth) | учётка дашборда открывает весь админ-API: `/api/pty` (терминал), `/api/files`, `/api/credentials`, `/api/env` — слишком много прав ради одной операции |
| Хранить имя треда только у себя, `title` в hermes не ставить | в CLI и TUI сессии остаются безымянными, `/resume <имя>` перестаёт работать |
| Различитель имени из `thread_id` | hermes не распознает его как вариант имени; `#N` — его родной формат |

## Порядок работ

1. ~~Таблица `hermes_profiles` и `HermesProfileRepository`~~ — сделано, 8 тестов
   на живой базе (`make pytest`).
2. ~~`HermesApiClient`~~ — сделано, 12 тестов на httpx-моке: префикс профиля,
   Bearer-заголовок, 409/404 как успех, 4xx → `InternalServiceError`,
   5xx и обрыв связи → `ExternalServiceError`.
3. ~~Конвертеры~~ — сделано, 27 тестов без сети: `HermesSessionCodec`,
   `HermesMessageCodec`, `HermesImageCodec` (картинки → элементы chainlit) и
   `HermesHistoryCodec` — единый вход, отдающий шаги и элементы вместе.
4. ~~`HermesDataLayer(BaseDataLayer)`~~ — сделано, 10 тестов на живой базе с
   мокнутым api_server: треды и история из hermes, остальное из postgres.
5. ~~Провайдер и подмена `get_data_layer`~~ — сделано: общий пул postgres на
   data layer и связку профилей, общий httpx на api_server, `hermes_data_layer`
   прогревается в bootstrap.
6. ~~`POST /api/profiles` в форке hermes и вызов из `create_user`~~ — сделано,
   10 тестов в форке (`tests/gateway/test_api_server_profiles.py`) и 5 здесь.
   Эндпоинт под API_SERVER_KEY заводит профиль клоном донора
   (`hermes.default_profile`), 409 при повторе; `create_user` зовёт его сразу
   после того, как закрепил имя профиля за пользователем в postgres.
7. ~~Замена langgraph-агента на SSE-поток сессии~~ — сделано, 18 тестов без сети:
   `HermesEventStream`/`HermesEventCodec` на кадрах живого api_server,
   `HermesApiClient.chat_stream` и `HermesTurn` (отрисовка хода в chainlit).
   Langchain убран из кода и зависимостей вместе с `CheckpointerConfig`,
   секцией `[checkpointer]` и трейсером; схему `checkpoints` в postgres не
   трогаем — в базе `boba` её нет, а в старой `n1bot` она пустая и общая с
   другими сервисами. Модель, ключ и base_url задаются теперь в `config.yaml`
   профиля hermes, поэтому `AgentProfile`/`OpenAiConfig` и секции
   `[agent]`/`[openai.*]` тоже удалены; настройки httpx-транспорта (limits,
   TCP keepalive, retries) переехали в `[hermes]`, а дамп запросов включается
   там же — `hermes.dump = true` пишет их в `<chainlit.root>/dump`.

## Ход агента: события `chat/stream`

Кадры SSE: `event: <имя>`, `data: <json>`, комментарии `: keepalive` и
`: stream closed`. Порядок и поля сняты с живого api_server.

| Событие | Поля | Что делает chainlit |
|---|---|---|
| `run.started` | `user_message` | ничего: сообщение уже показано |
| `message.started` | `message.id`, `message.role` | ничего |
| `assistant.delta` | `message_id`, `delta` | дописывает ответ токеном |
| `tool.started` | `tool_name`, `preview`, `args` | открывает шаг с `preview` на входе |
| `tool.completed` / `tool.failed` | `tool_name` (без `preview` и `args`) | закрывает шаг; `failed` помечает ошибкой |
| `tool.progress` | `tool_name` = `_thinking`, `delta` | ничего: рассуждение уже в истории |
| `assistant.completed` | `content`, `partial`, `interrupted` | ставит ответ, если дельт не было |
| `run.completed` | `messages`, `usage` | ничего: то же самое читается из истории |
| `error` | `message` | `AgentError`: код пользователю, текст в лог |
| `done` | — | конец потока |

Результат вызова инструмента в событиях не приходит — он остаётся в истории
сессии и виден при следующем открытии треда. Связки `tool.started` с
`tool.completed` у api_server нет (`message_id` общий на весь ход), поэтому
повторные вызовы одного инструмента закрываются в порядке открытия.

## Провижининг профиля

`POST /api/profiles` (без префикса `/p/<профиль>/`: операция уровня инстанса)
с телом `{"name", "clone_from", "description"}`. Профиль наследует от донора
`config.yaml`, `.env` и навыки — без них у него нет ни модели, ни ключа
провайдера. `profiles_to_serve()` читается на каждый запрос, поэтому
перезапуск gateway не нужен: свежесозданный профиль обслуживается сразу.

| Код | Когда | Что делает слой |
|---|---|---|
| 201 | профиль заведён | `create_profile` → `True` |
| 409 | профиль уже есть | `create_profile` → `False`, это не ошибка |
| 400 | имя не проходит `[a-z0-9][a-z0-9_-]{0,63}`, донора нет | `InternalServiceError` |
| 401 | нет или неверен `API_SERVER_KEY` | `InternalServiceError` |

Донор клонируется через `hermes_cli.profiles.create_profile(clone_config=True,
no_alias=True)`: alias-обёртки в `~/.local/bin` нужны CLI, а профиль из API
живёт только за http. Клонированный `config.yaml` при этом проходит миграцию
схемы hermes — донора нужно держать в той форме, которую понимает текущая
версия gateway, иначе у нового профиля не окажется рабочего провайдера.
