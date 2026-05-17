# local/

Runtime-данные и runtime-конфиг приложений (CLI и chainlit UI). Полностью
gitignored, в репо трекаются только шаблоны (`*.example`).

## Структура

| Путь | Что хранится |
|---|---|
| `.env` | Секреты, внешние URL, указатель на TOML (`BOBA_CONFIG_PATH`). |
| `config.toml` | Структурный конфиг (limits, paths, host/port, models). |
| `prompts/` | System-prompts агента. |
| `workspaces/` | Session workspaces агента. |
| `logs/` | Runtime-логи. |
| `chroma/` | ChromaDB persistent state. |
| `chainlit/` | Chainlit runtime-state (`.chainlit/`, `chainlit.md`, `public/`). |
| `docs/` | Документы для индексации в ChromaDB. |

## env vs TOML

Резолвер: `env-file > env > toml-file > toml` — env побеждает TOML.

- env (`.env`): `BOBA_CONFIG_PATH`, `BOBA_ADAPTER_OPENAI_BASE_URL`,
  `BOBA_ADAPTER_OPENAI_API_KEY`, `BOBA_CHAINLIT_AUTH_SECRET`,
  `BOBA_INDEXER__SOURCES__CONFLUENCE__BASE_URL`,
  `BOBA_INDEXER__SOURCES__CONFLUENCE__AUTH_TOKEN`.
- TOML (`config.toml`): всё остальное.

Имена ключей: `ConfigKey(parts...)` →
env `BOBA_<P1>_..._<PN>`, TOML `[<P1>...<P_{N-1}>] <PN>`.

## Onboarding

```bash
cp local/.env.example local/.env
cp local/config.toml.example local/config.toml
cp -r local/prompts.example local/prompts
# Установить пакеты монорепо в venv:
pip install -r dev-install.txt
```

В `local/.env` подставьте `BOBA_AGENT__API_KEY` и (опционально)
`BOBA_AGENT__BASE_URL`. В `local/config.toml` укажите `model` для
`[cli]` и `[chainlit]`.

## Запуск

`.env` сам не подгружается (это просто список `KEY=VALUE`), его надо
экспортировать в окружение перед запуском:

```bash
set -a && . local/.env && set +a
```

или через `direnv` (`echo 'dotenv local/.env' >> .envrc && direnv allow`).

### boba-cli-agent — REPL/single-shot

```bash
# REPL (если в [cli].query пусто):
.venv/bin/boba-cli-agent

# Single-shot (через argv, перекрывает [cli].query):
.venv/bin/boba-cli-agent --model qwen3.5-35b --query "hello"
```

CLI argv > env (`BOBA_CLI__…`) > TOML `[cli]`. REPL-команды: `/exit`,
`/quit`, `:q`, `/clear` (сбросить in-memory историю).

### boba-chainlit-agent — UI

```bash
.venv/bin/boba-chainlit-agent
```

Откроется на `http://<host>:<port>` из `[chainlit]` (по умолчанию
`0.0.0.0:8501`). Логин — `[chainlit].auth_username` / `auth_password`
(дефолт `admin/admin`). Каждый chainlit-thread = отдельный workspace
(history.jsonl per chat).

Конкретные пути к окружению (auth secret, app-state, workspaces) идут
из `local/config.toml` → `[chainlit].app_root` и `[agent].base_dir`.

## Mapping local-paths

Пути в TOML относительные, резолвятся от cwd:

- launch.json: `${workspaceFolder}/local/...`;
- docker-compose: bind mount `../boba/local → /app/local`.



## Индексация документов

`boba-cli-vector-index` пишет документы в Chroma. Агент потом читает их
через `kb_search` / `kb_list_collections` (read-only).

CLI выбирает **pipeline-плагин** по id (`--vector_index.pipeline=<id>`).
Pipeline — готовая сборка из RequestSource, Transport, Reader, Chunker и
Store. Параметры конкретного pipeline'а живут в его секции
`[indexer.pipelines.<id>]`.

### Confluence — URL и токен через env

```bash
export BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__BASE_URL=https://confl.loshara.com
export BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__AUTH_TOKEN=<PAT или пароль>
```

| Ключ | Значение |
|---|---|
| `BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__BASE_URL` | URL Confluence без trailing slash. |
| `BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__AUTH_TOKEN` | PAT (Atlassian) или пароль (для basic). |

В `local/config.toml`:

```toml
[indexer.pipelines.confluence_space]
auth_method = "pat"   # или "basic"
# auth_user = "alice" # только для basic
```

| Ключ | Значение |
|---|---|
| `auth_method` | `"pat"` — Bearer-токен. `"basic"` — login+password. |
| `auth_user` | Логин для `basic`; пустой при `pat`. |

### Индексация всего Confluence space

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=confl_docs \
  --vector_index.pipeline=ext.confluence_space \
  --indexer.pipelines.confluence_space.space_key=DOCS \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=index` | Записать в коллекцию. |
| `--vector_index.collection=confl_docs` | Имя коллекции в Chroma. Произвольное; создаётся при первом запуске. |
| `--vector_index.pipeline=ext.confluence_space` | Pipeline-плагин «индексация целого space». |
| `--indexer.pipelines.confluence_space.space_key=DOCS` | Ключ space'а в Confluence (часть URL `/display/DOCS/...`). |
| `--ext.chromadb.persist_path=./local/chroma` | Директория Chroma-store на диске. |

### Индексация .md из файловой системы

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=local_docs \
  --vector_index.pipeline=ext.fs_markdown \
  --indexer.pipelines.fs_markdown.paths=./local/manual \
  --indexer.pipelines.fs_markdown.include="*.md" \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.pipeline=ext.fs_markdown` | Pipeline-плагин «обход .md → heading-aware Section'ы → heading chunker → Chroma». |
| `--indexer.pipelines.fs_markdown.paths=./path/to/dir` | Список путей через запятую (файлы или директории). |
| `--indexer.pipelines.fs_markdown.include="*.md"` | Glob-фильтры включения (через запятую). Пусто — без фильтра. |

### Индексация .html из файловой системы

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=local_html \
  --vector_index.pipeline=ext.fs_html \
  --indexer.pipelines.fs_html.paths=./local/docs \
  --indexer.pipelines.fs_html.include="*.html,*.htm" \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.pipeline=ext.fs_html` | Pipeline-плагин «обход .html → heading-aware (по `<h1>..<h6>` + `id`) → heading chunker → Chroma». |

### Индексация .txt/.log из файловой системы

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=local_logs \
  --vector_index.pipeline=ext.fs_text \
  --indexer.pipelines.fs_text.paths=./local/logs \
  --indexer.pipelines.fs_text.include="*.txt,*.log" \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.pipeline=ext.fs_text` | Pipeline-плагин «UTF-8 plain-text → одна Section на файл → sliding chunker → Chroma». |

### Индексация явного списка Confluence-страниц

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=confl_pilot \
  --vector_index.pipeline=ext.confluence_pages \
  --indexer.pipelines.confluence_pages.page_ids=12345,67890 \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.pipeline=ext.confluence_pages` | Pipeline-плагин «явный список page-id'ов». |
| `--indexer.pipelines.confluence_pages.page_ids=12345,67890` | Page-id'ы через запятую. Page-id виден в URL: `?pageId=12345`. |

Для этого pipeline'а используются те же env: `BOBA_INDEXER__PIPELINES__CONFLUENCE_PAGES__BASE_URL` и `__AUTH_TOKEN`.

### Индексация по CQL-запросу

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=confl_recent \
  --vector_index.pipeline=ext.confluence_cql \
  --indexer.pipelines.confluence_cql.cql="space = DOCS AND lastModified > '2024-01-01'" \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.pipeline=ext.confluence_cql` | Pipeline-плагин «CQL-запрос». |
| `--indexer.pipelines.confluence_cql.cql="..."` | Любой валидный CQL. Примеры: `space = DOCS AND ancestor = 12345` (поддерево от страницы), `label = "api" AND space = DOCS` (по тегу), `space = DOCS AND lastModified > '2024-01-01'` (за период). |

env: `BOBA_INDEXER__PIPELINES__CONFLUENCE_CQL__BASE_URL` и `__AUTH_TOKEN`.

### Список коллекций

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=list \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=list` | Показать все коллекции в `persist_path`. |

`list/show/delete` не требуют `pipeline` — это admin-команды над Store.

### Просмотр содержимого коллекции

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=show \
  --vector_index.collection=confl_docs \
  --vector_index.show_limit=20 \
  --vector_index.show_snippet_chars=200 \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=show` | Read-only: вывести чанки коллекции (id, anchor, snippet). |
| `--vector_index.show_limit=20` | Сколько чанков показать (default 20). |
| `--vector_index.show_snippet_chars=200` | Длина превью текста на чанк (default 200). |

Чтобы посмотреть чанки только одного документа, добавь `show_source_id`:

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=show \
  --vector_index.collection=confl_docs \
  --vector_index.show_source_id="https://confl.loshara.com/pages/viewpage.action?pageId=12345" \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.show_source_id="..."` | Фильтр по полному `source_id`. Для Confluence: viewpage URL (`https://<host>/pages/viewpage.action?pageId=<id>`). Для FS: `fs:/abs/path`. С фильтром чанки выводятся в порядке `chunk_index`. |

### Sync — удалить чанки документов, исчезнувших из источника

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=sync \
  --vector_index.collection=confl_docs \
  --vector_index.pipeline=ext.confluence_space \
  --indexer.pipelines.confluence_space.space_key=DOCS \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=sync` | Сравнить source_id'ы из RequestSource с теми, что в Store; удалить чанки тех, которых уже нет в источнике. Требует `pipeline` (тот же, что использовался при index). |

### Удалить коллекцию целиком

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=delete \
  --vector_index.collection=confl_docs \
  --vector_index.confirm_skip=true \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=delete` | Удалить коллекцию из Chroma. |
| `--vector_index.confirm_skip=true` | Не спрашивать подтверждение (для скриптов). По умолчанию `false` — спросит `yes`. |

### Verbose

```bash
--vector_index.verbose=2
```

| Значение | Что логируется |
|---|---|
| `0` (default) | WARN: только ошибки и пропуски. |
| `1` | INFO: одна строка на документ + summary stats. |
| `2` | DEBUG: HTTP-запросы, парсинг heading'ов. |

### Output stats

После `index`:

```
collection='confl_docs' pipeline='ext.confluence_space'
sources_processed=42 sources_failed=0 sources_skipped_unchanged=128
sections_emitted=350 chunks_upserted=350 chunks_deleted=212
```

| Поле | Значение |
|---|---|
| `sources_processed` | Сколько новых/изменённых документов обработано. |
| `sources_failed` | Сколько упали с ошибкой (timeout, 404, parse-error); остальные доехали. |
| `sources_skipped_unchanged` | Сколько пропущено по incremental-skip (etag/version/mtime совпали). |
| `sections_emitted` | Всего Section'ов на выходе Reader'а. |
| `chunks_upserted` | Сколько чанков записано в Store. |
| `chunks_deleted` | Сколько старых чанков удалено перед upsert (idempotent re-index). |
