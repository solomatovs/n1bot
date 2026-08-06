# Ассеты интерфейса

Оформление и переводы chainlit-приложения. Лежат в репозитории и запекаются
в образ: сборщик кладёт их в `${BOBA_DIR}/data/`, откуда их читает chainlit
(`CHAINLIT_APP_ROOT`, значение берётся из `chainlit.root` конфига).

- `chainlit/config.toml` и `chainlit/translations/*.json` (в том числе русский
  `ru-RU.json`) — становятся `.chainlit/` внутри app_root;
- `chainlit/chainlit.md` — стартовая страница, ложится в корень app_root:
  в `.chainlit/` chainlit её не ищет;
- `public/` — тема `theme.json`, логотипы, favicon, аватары.

Каталог назван без точки: скрытые имена теряются при упаковке и попадают
под общие ignore-правила. Точку добавляет сборщик.
