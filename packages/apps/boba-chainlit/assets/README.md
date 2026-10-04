# Ассеты интерфейса

Оформление и переводы chainlit-приложения. Каталог сам по себе — готовый
app_root: файлы лежат ровно там, где их ищет chainlit (`CHAINLIT_APP_ROOT`,
значение берётся из `chainlit.root` конфига).

- `.chainlit/config.toml` и `.chainlit/translations/*.json` (в том числе русский
  `ru-RU.json`) — настройки и переводы интерфейса;
- `chainlit.md` — стартовая страница; в `.chainlit/` chainlit её не ищет;
- `public/` — тема `theme.json`, логотипы, favicon, аватары.

Развёртывание копирует набор в app_root образа (Dockerfile) и релиза
(`make release`). Отладочный запуск читает каталог напрямую: `BOBA_APP_ROOT`
в launch.json указывает сюда, и правка ассета видна без пересборки. Рядом
появляется `.files` — рабочий каталог вложений chainlit, он под ignore-правилом.
