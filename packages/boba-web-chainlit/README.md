# boba-chainlit

Chainlit web UI для Boba-агента. Запускает FastAPI-стек, рендерит чат,
держит сессию пользователя в `cl.user_session`, мостит события агента
в Chainlit через `ChainlitBridgeSink`.

## Установка

Сначала core (если ещё не установлен):
```bash
pip install -e .                    # из корня репо
```

Затем UI:
```bash
pip install -e ./packages/boba-chainlit
```

## Запуск

Через entry-point (рекомендуется — bootstrap env-оверрайдов выполняется
до импорта chainlit):
```bash
boba-chainlit
```

Через Chainlit CLI напрямую (минуя bootstrap, требует ручной выставки
`CHAINLIT_*` env):
```bash
chainlit run packages/boba-chainlit/src/boba_chainlit/app.py -h --host 0.0.0.0 --port 8501
```

## Конфигурация

Поверх обычного `boba.infra.config.ConfigLoader`-окружения добавляются
chainlit-specific:

| env | назначение |
|---|---|
| `CHAINLIT_HOST` | host bind, default из `chainlit_resolver()` |
| `CHAINLIT_PORT` | port |
| `CHAINLIT_ROOT_PATH` | URL префикс при reverse-proxy |
| `CHAINLIT_AUTH_SECRET` | для подписи сессионных cookies |
| `CHAINLIT_HEADLESS` | `true` отключает auto-open browser |
| `BOBA_UI_*` (см. `_ui_overrides.py`) | оверрайды UI-config из env в `.chainlit/config.toml` |

## Запуск из VSCode

См. [.vscode/launch.json](../../.vscode/launch.json), конфигурация
**`Chainlit: Boba UI`**.
