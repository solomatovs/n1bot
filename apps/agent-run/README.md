# boba-cli-agent-run

Operator CLI: собирает полный agent stack (prompts, tools, LLM-клиент,
workspace) и прогоняет один пользовательский запрос с выводом всех
событий в stdout/stderr через `ConsoleSink`.

Использование — отладка/демонстрация локально, smoke-проверка при
деплое. В UI (chainlit) агент собирается тем же `boba.infra.container`,
поэтому поведение CLI полностью эквивалентно прогону через chat.

## Установка

Сначала core (если ещё не установлен):
```bash
pip install -e .                    # из корня репо: ставит boba editable + все runtime deps
```

Затем сам CLI:
```bash
pip install -e ./apps/agent-run
```

После этого `boba-cli-agent-run` работает из терминала без `PYTHONPATH`.

## Запуск

```bash
boba-cli-agent-run --model qwen2.5-14b "Прочитай файл X и расскажи о чём он"
```

Опциональные sampling-параметры:

| Флаг | Назначение |
|---|---|
| `--temperature 0.2` | Креативность модели (0..1) |
| `--top-p 0.95` | Nucleus sampling threshold |
| `--max-tokens 2048` | Лимит на длину ответа |
| `--seed 42` | Детерминизм |
| `--stop "###"` | Stop-последовательность (можно повторять) |
| `--frequency-penalty 0.0` | Штраф за повторы |
| `--presence-penalty 0.0` | Штраф за уже упомянутые токены |

Если ни один не задан — агент шлёт запрос без `sampling`-блока, модель
использует свои дефолты.

## Конфигурация

Берётся через `boba.infra.config.ConfigLoader` — те же env-переменные,
что и chainlit-runtime: `BOBA_PROMPTS_DIR`, `WORKSPACE_BASE_DIR`,
`LLM_BASE_URL`, `LITELLM_API_KEY` (или `LITELLM_API_KEY_FILE`),
`BOBA_CONFIG` (опциональный TOML), плюс namespaced
`BOBA_EXT_<NAMESPACE>__*` для pip-installed extension-пакетов.

## Запуск из VSCode

См. [.vscode/launch.json](../../.vscode/launch.json), конфигурация
**`Agent: run query`** — попросит выбрать модель и запрос, прогонит
агент через editable-install в `.venv`.
