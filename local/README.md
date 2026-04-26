# local/

Runtime-данные и **runtime-конфиг** всех приложений (CLI и chainlit UI)
— в этой директории. Полностью **gitignored**, в репо трекаются только
шаблоны (`*.example`, `*.example/...`) и `.gitkeep`-маркеры пустых
поддиректорий.

## Структура

| Путь | Что хранится | Кто пишет |
|---|---|---|
| `.env` | Secrets + tunables (LLM_BASE_URL, LITELLM_API_KEY, AGENT_*, CHAINLIT_*). Загружается через `envFile`/`env_file` в launch.json и docker-compose. | оператор |
| `config.toml` | Опциональный TOML для `BOBA_CONFIG`. Альтернатива/дополнение к `.env`. | оператор |
| `prompts/` | System-prompts агента (read'аются `BOBA_PROMPTS_DIR`). | оператор (это его конфиг behavior'а агента) |
| `workspaces/` | Session workspaces агента (project + history + scratch). | runtime: chainlit, agent-run |
| `logs/` | Runtime-логи (`chainlit.log`, `agent-run.log`, `tests.log`). | runtime: все приложения |
| `chroma/` | ChromaDB persistent state (vector store). | runtime: vector-index CLI пишет, chromadb-extension читает |
| `docs/` | Документы (`.md`/`.txt`) для индексации в ChromaDB. По умолчанию `boba-cli-vector-index index` смотрит сюда. | оператор |

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
# отредактировать local/.env: указать LLM_BASE_URL, LITELLM_API_KEY, ...
# отредактировать local/prompts/*.md под свои нужды
```

После этого все launch.json-конфигурации (`Agent: run query`,
`Vector Index: ...`, `Chainlit: Boba UI`) подхватят пути к `local/*`
автоматически.

## Mapping local-paths

В **launch.json** пути к state используют префикс
`${workspaceFolder}/local/` — например,
`WORKSPACE_BASE_DIR=${workspaceFolder}/local/workspaces`.

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
