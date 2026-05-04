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
```

## Mapping local-paths

Пути в TOML относительные, резолвятся от cwd:

- launch.json: `${workspaceFolder}/local/...`;
- docker-compose: bind mount `../boba/local → /app/local`.



## Индексация документов

`boba-cli-vector-index` пишет документы в Chroma. Агент потом читает их
через `kb_search` / `kb_list_collections` (read-only).

### Confluence — URL и токен через env

```bash
export BOBA_INDEXER__SOURCES__CONFLUENCE__BASE_URL=https://confl.loshara.com
export BOBA_INDEXER__SOURCES__CONFLUENCE__AUTH_TOKEN=<PAT или пароль>
```

| Ключ | Значение |
|---|---|
| `BOBA_INDEXER__SOURCES__CONFLUENCE__BASE_URL` | URL твоего Confluence без trailing slash. |
| `BOBA_INDEXER__SOURCES__CONFLUENCE__AUTH_TOKEN` | PAT (Atlassian) или пароль (для basic). |

В `local/config.toml`:

```toml
[indexer.sources.confluence]
auth_method = "pat"   # или "basic"
# auth_user = "alice" # только для basic
```

| Ключ | Значение |
|---|---|
| `auth_method` | `"pat"` — Bearer-токен. `"basic"` — login+password. |
| `auth_user` | Логин для `basic`; пустой при `pat`. |

### Индексация всего space

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=confl_docs \
  --vector_index.source=ext.confluence_space \
  --indexer.sources.confluence.space.space_key=PAAS \
  --ext.chromadb.persist_path=./local/paas
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=index` | Записать в коллекцию. |
| `--vector_index.collection=confl_docs` | Имя коллекции в Chroma. Произвольное; создаётся при первом запуске. |
| `--vector_index.source=ext.confluence_space` | Использовать Source-плагин «весь space». |
| `--indexer.sources.confluence.space.space_key=DOCS` | Ключ space'а в Confluence (часть URL `/display/DOCS/...`). |
| `--ext.chromadb.persist_path=./local/chroma` | Директория Chroma-store на диске. |

### Индексация одной/нескольких страниц

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=confl_pages \
  --vector_index.source=ext.confluence_pages \
  --indexer.sources.confluence.pages.page_ids=12345,67890 \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.source=ext.confluence_pages` | Source-плагин «явный список страниц». |
| `--indexer.sources.confluence.pages.page_ids=12345,67890` | Page-id'ы через запятую. Page-id виден в URL Confluence: `?pageId=12345`. |

### Индексация по CQL-запросу

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=index \
  --vector_index.collection=confl_recent \
  --vector_index.source=ext.confluence_cql \
  --indexer.sources.confluence.cql.cql="space = DOCS AND lastModified > '2024-01-01'" \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.source=ext.confluence_cql` | Source-плагин «по CQL-запросу». |
| `--indexer.sources.confluence.cql.cql="..."` | CQL — Confluence Query Language. Примеры: `space = DOCS AND ancestor = 12345` (поддерево от страницы), `label = "api" AND space = DOCS` (по тегу), `space = DOCS AND lastModified > '2024-01-01'` (за период). |

### Список коллекций

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=list \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=list` | Показать все коллекции в `persist_path`. |

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
  --vector_index.show_source_id="confluence://confl.loshara.com/page/12345" \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.show_source_id="..."` | Фильтр по полному `source_id`. Для Confluence: `confluence://<host>/page/<page_id>`. Для FS: `fs:/abs/path`. С фильтром чанки выводятся в порядке `chunk_index`. |

### Sync — удалить чанки страниц, которых больше нет в Confluence

```bash
.venv/bin/boba-cli-vector-index \
  --vector_index.action=sync \
  --vector_index.collection=confl_docs \
  --vector_index.source=ext.confluence_space \
  --indexer.sources.confluence.space.space_key=DOCS \
  --ext.chromadb.persist_path=./local/chroma
```

| Ключ | Значение |
|---|---|
| `--vector_index.action=sync` | Сравнить page-id'ы из Source с теми, что в Store; удалить чанки тех, которых нет в Source. |

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
| `1` | INFO: одна строка на страницу. |
| `2` | DEBUG: HTTP-запросы, парсинг heading'ов. |
