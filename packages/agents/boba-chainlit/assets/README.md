# Ассеты интерфейса

Оформление и переводы chainlit-приложения. Каталог сам по себе — готовый
app_root: файлы лежат ровно там, где их ищет chainlit (`CHAINLIT_APP_ROOT`,
значение берётся из `chainlit.root` конфига).

- `.chainlit/config.toml` и `.chainlit/translations/*.json` (в том числе русский
  `ru-RU.json`) — настройки и переводы интерфейса;
- `chainlit.md` — стартовая страница; в `.chainlit/` chainlit её не ищет;
- `public/` — тема `theme.json`, логотипы, favicon, аватары.

Dockerfile копирует набор в app_root образа. Отладочный запуск читает каталог
напрямую: `BOBA_APP_ROOT` в `.vscode/launch.json` указывает сюда. Изменения
статических файлов видны без пересборки; TypeScript и React собирает
`make -C build web`, который кладёт результат в этот же каталог. Писать
сюда приложение не должно: вложения сессий chainlit уходят в каталог
`[chainlit].files_dir` (в данных приложения), а не в `.files` рядом с ассетами.
