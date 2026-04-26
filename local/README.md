# local/

Runtime-данные и **runtime-конфиг** всех приложений (CLI и chainlit UI)
— в этой директории. Полностью **gitignored**, в репо трекаются только
шаблоны (`*.example`, `*.example/...`) и `.gitkeep`-маркеры пустых
поддиректорий.

## Структура

| Путь | Что хранится | Кто пишет |
|---|---|---|
| `.env` | Secrets + tunables под единым префиксом `BOBA_*` (`BOBA_LLM_BASE_URL`, `BOBA_LLM_API_KEY`, `BOBA_AGENT_*`, `BOBA_CHAINLIT_*`, …). Загружается через `envFile`/`env_file` в launch.json и docker-compose. | оператор |
| `config.toml` | Опциональный TOML; путь хранится в `BOBA_CONFIG_PATH`. Альтернатива/дополнение к `.env`. | оператор |
| `prompts/` | System-prompts агента (читаются по `BOBA_PROMPTS_DIR`). | оператор (это его конфиг behavior'а агента) |
| `workspaces/` | Session workspaces агента (project + history + scratch). Корневая директория — `BOBA_WORKSPACES_BASE_DIR`. | runtime: chainlit, agent-run |
| `logs/` | Runtime-логи (`chainlit.log`, `agent-run.log`, `tests.log`). Файл — `BOBA_APP_LOG_FILE`. | runtime: все приложения |
| `chroma/` | ChromaDB persistent state (vector store). Путь — `BOBA_EXT_CHROMADB_PERSIST_PATH`. | runtime: vector-index CLI пишет, chromadb-extension читает |
| `docs/` | Документы (`.md`/`.txt`) для индексации в ChromaDB. По умолчанию `boba-cli-vector-index index` смотрит сюда. | оператор |

Имена ключей задаются единым алгоритмом (см.
`packages/boba-config-env/src/boba_config_env/_source.py`):
`ConfigKey(parts...)` → `BOBA_<P1>_<P2>_..._<PN>` (uppercase, через `_`).
TOML-вариант: `parts[:-1]` → секция, `parts[-1]` → leaf-ключ
(`packages/boba-config-toml/src/boba_config_toml/_source.py`).

Шаблонные дефолты для конфига:

| Шаблон | Куда копировать | Зачем |
|---|---|---|
| `.env.example` | `.env` | Стартовый набор переменных |
| `config.toml.example` | `config.toml` (опц.) | Альтернатива .env через TOML |
| `prompts.example/` | `prompts/` | Дефолтные system-prompt блоки агента |

## Onboarding

```bash
cp local/.env.example local/.env
cp -r local/prompts.example local/prompts
# отредактировать local/.env: задать BOBA_LLM_BASE_URL, BOBA_LLM_API_KEY, ...
# отредактировать local/prompts/*.md под свои нужды
```

После этого все launch.json-конфигурации (`Agent: run query`,
`Vector Index: ...`, `Chainlit: Boba UI`) подхватят пути к `local/*`
автоматически.

## Mapping local-paths

В **launch.json** пути к state используют префикс
`${workspaceFolder}/local/` — например,
`BOBA_WORKSPACES_BASE_DIR=${workspaceFolder}/local/workspaces`.

В **docker-compose.yml** контейнер монтирует `../boba/local` →
`/app/local` (bind mount), и env-переменные используют префикс
`/app/local/`. То есть **те же файлы** на диске, разные абсолютные
пути внутри/снаружи контейнера.

Это позволяет запускать `boba-cli-vector-index index ...` локально и
видеть результаты в chainlit-контейнере без копирования данных.

## Что НЕ кладём в local/

- **Исходный код** — в `packages/`.
- **VSCode launch/settings** — в `.vscode/`. Это IDE-настройки, не
  runtime-state.
