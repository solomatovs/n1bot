# Руководство администратора boba

Boba состоит из двух HTTP-приложений: chainlit (чат, порт 8501) и studio
(API, страницы workflow и каталога, порт 8502). У каждого свой каталог
установки со своим `conf/config.toml`, оба работают над одной базой Postgres
и одной таблицей соединений, поэтому часть настроек обязана совпадать.
Документ описывает, что настраивает администратор и как это проверить.

Разделы без содержимого помечены как заготовки: тема есть, текст будет
дописан.

## 1. Состав и раскладка

Заготовка: дерево установки (`app`, `third`, `app_root`, `conf`, `data`,
`sandbox`, `models`), что переживает релиз, что заменяется.

## 2. Установка и запуск

Заготовка: установка дерева релиза в `INSTALL_DIR`, systemd-юниты
`boba-chainlit.service` и `boba-studio.service`, `cgroup-init.sh`, порядок
первого старта.

## 3. Конфигурация

Заготовка: слои конфига (`[env]` в toml, `conf/plugins/*.toml`,
переопределения `BOBA_*` из окружения), назначение секций `config.toml`
chainlit и studio, что должно совпадать между приложениями.

## 4. Секреты

Оба секрета лежат в секции `[site]` каждого `config.toml` и подставляются
ссылками в рабочие секции. Значения обязаны быть одинаковыми в chainlit и
studio: chainlit выдаёт cookie входа, studio её проверяет; оба приложения
читают одну таблицу соединений.

### `auth_secret`

Куда подставляется: `[session].auth_secret`.

Что делает:

- подписывает JWT входа (HS256), который едет в cookie `access_token`;
- запечатывает kerberos-билет пользователя внутри этого JWT (ключ выводится
  из секрета через HKDF-SHA256, шифр Fernet);
- уходит в chainlit как `CHAINLIT_AUTH_SECRET` для его собственных сессий.

Требования: непустая строка. Формат не проверяется, но секрет должен быть
случайным и длинным, не короче 32 байт энтропии.

Сгенерировать:

```
openssl rand -base64 48
```

или

```
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Смена секрета: все выданные cookie становятся недействительными, пользователи
входят заново. Данные не теряются. Менять одновременно в обоих приложениях,
иначе studio будет отвергать cookie chainlit.

### `database_encryption_key`

Куда подставляется: `[connections].encryption_key`.

Что делает: шифрует секреты сохранённых соединений пользователей (пароли,
токены, keytab) перед записью в таблицу соединений. Шифр Fernet, значение в
базе хранится с префиксом `enc:v1:`.

Требования: ровно 32 байта в стандартном base64 (44 символа с `=` на
конце). Проверяется при старте: другая длина или не-base64 останавливают
приложение с ошибкой `[connections].encryption_key: expected 32 bytes in
base64`.

Сгенерировать:

```
openssl rand -base64 32
```

или

```
python3 -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Смена ключа: сохранённые секреты соединений перестают расшифровываться,
инструмента перешифрования нет. Пользователям придётся заново ввести
пароли и ключи в своих соединениях. Ключ генерируется один раз при
установке и дальше только переносится между релизами.

## 5. Аутентификация и роли

Заготовка: `[auth.kerberos]`, `[auth.ldap]`, `[auth.local]`, `[role.ldap]`,
`[roles]`, keytab HTTP-сервиса, `[session]` (срок cookie, `cookie_samesite`
и когда нужен `none`).

### Разрешённые origin (CORS)

Файл `app_root/.chainlit/config.toml` chainlit, ключ `allow_origins` в секции
`[project]`. По умолчанию стоит `["*"]`. Запросы к chainlit идут с cookie
входа, и при `*` сервер подставляет в ответ origin любого запроса: чужой сайт
сможет ходить в chainlit от имени вошедшего пользователя. Перечислять только
свои адреса, схема и хост целиком:

```toml
allow_origins = ["https://boba.company.ru"]
```

Если виджет copilot встраивается на другой сайт, его адрес добавляется в тот
же список:

```toml
allow_origins = ["https://boba.company.ru", "https://portal.company.ru"]
```

Список действует на HTTP-запросы. Рукопожатие websocket проверке origin в
chainlit не подчиняется, поэтому список не заменяет остальные меры: вход по
cookie и `cookie_samesite`.

## 6. Соединения и инструменты

Заготовка: таблица соединений, гранты, плагины `conf/plugins/*.toml`,
списки `tools` и `headless`, роли на инструменты. Разработка новых
соединений и инструментов описана в
[adding-connections-and-tools.md](adding-connections-and-tools.md).

## 7. Песочница и лимиты

Заготовка: `[tool_launcher]` (sandbox или process), образы `rootfs.ext4`
плагинов, `workspace.ext4`, cgroup-лимиты, `/dev/fuse` и user namespace.

## 8. Nginx перед boba

Оба приложения развёрнуты на своих префиксах (`/boba/`, `/boba-studio/`) и
ожидают путь целиком, без срезания префикса. Nginx перед ними должен уметь
три вещи: пропускать веб-сокеты, не буферизовать потоковые ответы и
принимать заголовки крупнее дефолтных восьми килобайт.

### Конфигурация

```nginx
server {
  listen 443 ssl;
  server_name boba.example.com;

  large_client_header_buffers 4 32k;
  port_in_redirect off;

  location /boba/ {
    resolver 127.0.0.53 valid=5s;
    set $chainlit http://boba-chainlit.internal:8501;
    proxy_pass $chainlit;

    include /etc/nginx/conf.d/options/boba-headers.conf;
  }

  location /boba-studio/ {
    resolver 127.0.0.53 valid=5s;
    set $studio http://boba-studio.internal:8502;
    proxy_pass $studio;

    include /etc/nginx/conf.d/options/boba-headers.conf;
  }
}
```

`conf.d/options/boba-headers.conf`:

```nginx
proxy_http_version 1.1;
proxy_cache_bypass $http_upgrade;

proxy_set_header Upgrade           $http_upgrade;
proxy_set_header Connection        "upgrade";
proxy_set_header Host              $http_host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $http_host;
proxy_set_header X-Forwarded-Port  $server_port;

proxy_buffering         off;
proxy_request_buffering off;

proxy_buffer_size       32k;
proxy_buffers           8 32k;
proxy_busy_buffers_size 64k;

proxy_read_timeout 86400s;
```

### Параметры

- **`proxy_http_version 1.1` и заголовки `Upgrade`/`Connection`.** По умолчанию
  nginx ходит к бэкенду по HTTP/1.0, где нет механизма Upgrade. Без этих
  строк веб-сокет чата не поднимется: приложение ответит ошибкой рукопожатия.
- **`proxy_cache_bypass $http_upgrade`.** Запрос на смену протокола не должен
  отдаваться из кэша.
- **`Host`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `X-Forwarded-Port`.**
  Приложение строит абсолютные ссылки и редиректы по этим заголовкам. Без
  `X-Forwarded-Proto` редиректы уйдут на `http://` и войдут в цикл. `$http_host`
  вместо `$host`: первый передаёт заголовок как есть, второй отбрасывает порт.
- **`X-Real-IP`, `X-Forwarded-For`.** Адрес клиента для логов и ограничений.
- **`proxy_buffering off`, `proxy_request_buffering off`.** Чат отдаёт ответ
  модели потоком, и каждая порция должна доезжать сразу. С буферизацией
  ответ приходит рывками или застревает до конца хода. Выключенная
  буферизация запроса нужна, чтобы загрузка файла шла в приложение сразу.
- **`proxy_buffer_size 32k`, `proxy_buffers 8 32k`, `proxy_busy_buffers_size 64k`.**
  JWT входа несёт билет доменной аутентификации и едет тремя cookie общим
  объёмом около десяти килобайт. Дефолтные буферы рассчитаны на восемь
  килобайт заголовков ответа, и вход через SSO падает с 400 или 431.
- **`large_client_header_buffers 4 32k`.** То же самое для запроса от браузера.
  Стоит на уровне `server`. Нужны обе директивы: поднять одну и оставить
  другую даёт ту же ошибку с другой стороны.
- **`port_in_redirect off`.** Запрос `/boba` без завершающего слэша nginx сам
  отвечает редиректом на `/boba/` и по умолчанию подставляет в адрес порт,
  на котором слушает. Если nginx стоит за другим прокси или stream-мультиплексором
  и слушает не 443, редирект уведёт клиента на закрытый снаружи порт.
- **`proxy_read_timeout 86400s`.** Веб-сокет чата и длинные ходы модели не
  должны обрываться по таймауту простоя.
- **`resolver` и `proxy_pass` через переменную.** Nginx резолвит имя из
  `proxy_pass` один раз при старте. Если адрес приложения сменился, он
  ходит на старый и отдаёт 502 до перезагрузки. С переменной имя резолвится
  на каждый запрос через указанный DNS с кэшем в пять секунд; в примере это
  локальный systemd-resolved, подставьте свой. Если приложение задано
  IP-адресом, пара `resolver`/`set` не нужна, `proxy_pass` пишется напрямую.
  Побочный эффект переменной: nginx не срезает префикс пути, и приложение
  получает путь целиком. Для boba это и нужно.

### Диагностика

| Симптом | Причина | Что смотреть |
|---|---|---|
| 502 после смены адреса приложения | адрес закэширован при старте | пара `resolver`/`set` в `location` |
| Веб-сокет не поднимается | нет `Upgrade`/`Connection` или два заголовка `Connection` в блоке | `proxy_set_header` в этом `location` |
| Ответ модели приходит рывками | включена буферизация или gzip для пути | `proxy_buffering`, `gzip_types` |
| 400 или 431 после входа через SSO | заголовки не помещаются в буферы | `large_client_header_buffers`, `proxy_buffer_size` |
| Редиректы уходят на `http://` | нет `X-Forwarded-Proto` | заголовки в `boba-headers.conf` |
| Редирект на `/boba/` уходит с чужим портом | nginx слушает не 443 и подставляет свой порт | `port_in_redirect off` в `server` |
| Приложение видит всех клиентов как один адрес | `X-Real-IP` не передан или перед nginx ещё один прокси | `X-Real-IP`, `set_real_ip_from` |

### Применение правок

```
nginx -t
nginx -s reload
```

## 9. Диагностика приложения

Заготовка: `journalctl -u boba-chainlit`, `journalctl -u boba-studio`,
`BOBA_LOG_LEVEL`, типовые ошибки старта (kerberos, база, ключи), проверка
релиза `bobacheck.py`.
