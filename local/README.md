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

- env (`.env`): `BOBA_CONFIG_PATH`, `BOBA_LLM_BASE_URL`, `BOBA_LLM_API_KEY`,
  `BOBA_CHAINLIT_AUTH_SECRET`.
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
