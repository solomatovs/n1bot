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

### `generation`

Где лежит: `[session].generation` каждого приложения.

Что делает: поколение сессий. Значение попадает в metadata входа и в
токен, при чтении токена сверяется с текущим. Токен другого поколения
отвергается: `401`, cookie снимается, страница уходит на вход.

Два режима:

- пустая строка: при каждом старте процесса генерируется случайное значение,
  рестарт приложения разлогинивает всех его пользователей;
- заданное значение: используется как есть и переживает рестарты; менять его
  вручную, чтобы разлогинить всех.

Chainlit выдаёт cookie входа, studio её проверяет. Если приложения должны
принимать токены друг друга, `generation` у них задаётся одинаковой строкой.
Пустое значение у обоих означает две независимые сессии: вход в чат не даёт
входа в studio. Токены партнёров, выпущенные напрямую по `auth_secret`,
поколения не несут и отвергаются: для них есть вход proxy.

## 5. Аутентификация и роли

Заготовка: `[auth.kerberos]`, `[auth.ldap]`, `[auth.local]`, `[role.ldap]`,
`[roles]`, keytab HTTP-сервиса, `[session]` (срок cookie, `cookie_samesite`
и когда нужен `none`).

### Токен входа (JWT)

Итог любого входа — один JWT в cookie с именем из `[session].cookie` (обычно
`access_token`). Токен состоит из трёх частей через точку: заголовок,
полезная нагрузка, подпись HS256 секретом `[session].auth_secret`. Первые две
части — обычный base64url, то есть **тело токена читается кем угодно без
всякого ключа**; подпись защищает от подделки, а не от чтения.

Поля верхнего уровня полезной нагрузки называются claims. Наши:

| Поле | Что значит |
|---|---|
| `identifier` | логин в нижнем регистре, ключ строки users |
| `display_name` | логин как набран, для интерфейса |
| `metadata` | всё, что вход знает о себе (таблица ниже) |
| `exp` | до какого момента токен годен, unix-время |
| `iat` | когда выпущен |
| `since` | когда начался первый вход этой сессии; по нему считается потолок продления |

Внутри `metadata`:

| Ключ | Что значит |
|---|---|
| `provider` | каким провайдером выпущен вход: `KerberosAuth`, `LdapAuth`, `LocalAuth`, `ProxyAuth` |
| `principal` | принципал kerberos-входа |
| `roles` | роли, посчитанные при входе |
| `profiles` | профили, доступные входу |
| `profile` | профиль, выбранный входом для новых чатов; только у proxy с заголовком `selected` |
| `sso_ticket` | делегированный kerberos-билет под шифром |
| `generation` | поколение сессий, при котором выпущен вход |

Целиком токен выглядит так:

```json
{
  "identifier": "ivanov",
  "display_name": "Ivanov",
  "metadata": {
    "provider": "KerberosAuth",
    "principal": "ivanov@LOSHARA.COM",
    "roles": ["analyst"],
    "profiles": ["default"],
    "sso_ticket": "gAAAAABm…",
    "generation": "3f9c1a…"
  },
  "exp": 1757500000,
  "iat": 1757496400,
  "since": 1757490000
}
```

Роли и профили лежат в токене, а не берутся из базы на каждый запрос: правки
`[roles]` и `[profiles.X].roles` действуют с нового входа, у kerberos — с
ближайшего обновления сессии.

Секретное в токене одно — `sso_ticket`, и он зашифрован Fernet на ключе,
выведенном из `auth_secret`: в теле токена видна только непрозрачная строка
`gAAAAAB…`, открыть её может лишь процесс с тем же секретом. Ничего другого
секретного в токен не кладётся. Настройки пользователя (`llm`,
`studio_profile`) в токен не едут — они живут в колонке `metadata` строки
users.

Cookie ставится с `HttpOnly`, то есть JavaScript страницы её не прочитает, но
владелец браузера видит её в devtools. Kerberos-билет делает токен длинным,
поэтому cookie больше 3000 символов режется на `access_token_0`,
`access_token_1` и так далее и собирается обратно при чтении; в браузере видны
несколько cookie вместо одной — это норма.

Посмотреть содержимое токена:

```
python3 -c "import base64,json,sys; p=sys.argv[1].split('.')[1]; print(json.dumps(json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4))),indent=2,ensure_ascii=False))" <токен>
```

Токен отвергается в четырёх случаях: истёк `exp`, подпись не сходится с
`auth_secret`, тело не разбирается, поколение не совпадает с текущим
(см. `generation`). Во всех случаях ответ `401`, cookie снимается, страница
уходит на вход.

### Вход и запросы с токеном: примеры curl

Адреса: `B` — chainlit с префиксом, например `https://boba.company.ru/boba`,
`S` — api studio, например `https://boba.company.ru/boba-studio/api/v1`.
Cookie входа во всех примерах сохраняется в `cookies.txt` ключом `-c` и
отдаётся обратно ключом `-b`; это только удобство curl, сервер видит обычный
заголовок `Cookie`.

**Логин и пароль**, пользователь из `[auth.local]` или `[auth.ldap]`.
Chainlit принимает форму:

```bash
curl -s -c cookies.txt -X POST "$B/login" \
  -d "username=portal-bot" -d "password=$BOBA_PASSWORD"
```

Ответ `200` и `{"success":true}`. Studio принимает JSON и требует метку своего
запроса, без неё `403`:

```bash
curl -s -c cookies.txt -X POST "$S/auth/login" \
  -H "x-boba-request: 1" -H "Content-Type: application/json" \
  -d '{"username":"portal-bot","password":"'"$BOBA_PASSWORD"'"}'
```

Ответ `204`, cookie в заголовке `Set-Cookie`.

**Kerberos SSO**, доменная учётка с keytab и curl с GSSAPI. Билет получать
вручную не нужно: libkrb5 сам берёт TGT из keytab по `KRB5_CLIENT_KTNAME`.

```bash
KRB5_CLIENT_KTNAME=/etc/portal/portal-bot.keytab \
curl -s -c cookies.txt --negotiate -u : "$B/auth/sso"
```

Ответ `303` на страницу чата, cookie в этом же ответе. `401` с
`WWW-Authenticate: Negotiate` означает, что билет не выдан: проверить keytab,
`krb5.conf` и DNS до KDC. В studio тот же обмен на `$S/auth/sso`.

**Доверенный заголовок (proxy)**, см. раздел ниже про подпись:

```bash
TS=$(date +%s)
SIG=$(printf 'portal-bot:%s:ops:general,search:search' "$TS" | openssl dgst -sha256 -hmac "$PROXY_SECRET" | awk '{print $NF}')

curl -s -c cookies.txt -X POST "$B/auth/proxy" \
  -H "X-Remote-User: portal-bot" \
  -H "X-Remote-Roles: ops" \
  -H "X-Remote-Profiles: general,search" \
  -H "X-Remote-Profile: search" \
  -H "X-Boba-Timestamp: $TS" \
  -H "X-Boba-Signature: $SIG"
```

Ответ `204`. В studio тот же маршрут `$S/auth/proxy`.

**Готовый JWT**, подписанный `auth_secret`. Так работает виджет copilot; секрет
наружу не отдавать, для партнёров есть proxy:

```bash
curl -s -c cookies.txt -X POST "$B/auth/jwt" -H "Authorization: Bearer $JWT"
```

**Как отдавать токен дальше.** Все маршруты, кроме `/auth/jwt`, ищут сессию
только в cookie; `Authorization: Bearer` они не принимают. Значение после
`access_token=` и до `;` в `Set-Cookie` и есть JWT:

```bash
T=$(awk '$6 ~ /^access_token(_[0-9]+)?$/ {print $6, $7}' cookies.txt | sort -V | awk '{printf "%s", $2}')
```

Команда собирает и цельную cookie, и чанки `access_token_0`, `access_token_1`
по порядку. Дальше либо файл cookie, либо заголовок вручную:

```bash
curl -s -b cookies.txt "$B/user"
curl -s "$B/user" -H "Cookie: access_token=$T"
```

Ответ `200` с пользователем и его `metadata`, `401` без сессии или с сессией
другого поколения. Если токен пришёл чанками, заголовок перечисляет их все:
`Cookie: access_token_0=…; access_token_1=…`.

Studio, пользователь, его роли, профиль и список доступных профилей:

```bash
curl -s -b cookies.txt "$S/me"
curl -s -b cookies.txt "$S/profiles"
curl -s -b cookies.txt -X PUT "$S/me/profile" \
  -H "Content-Type: application/json" -d '{"profile":"search"}'
```

**Продление и выход.** Продление зовёт скрипт страницы по сигналу сервера,
руками оно нужно редко:

```bash
curl -s -b cookies.txt -c cookies.txt -X POST "$B/auth/refresh" -H "x-boba-request: 1"
```

Для парольного и proxy-входа это перевыпуск JWT, ответ `204` и новая cookie;
для kerberos это повторный SPNEGO-обмен, добавить `--negotiate -u :`. Выход
снимает cookie:

```bash
curl -s -b cookies.txt -X POST "$B/logout"
curl -s -b cookies.txt -X POST "$S/auth/logout" -H "x-boba-request: 1"
```

Не работают: `/auth/header` и OAuth chainlit, boba регистрирует в нём только
парольный callback и свои маршруты.

### Провайдеры ролей и профилей

Роли и профили пользователя считаются при входе и уходят в токен вместе с
личностью. У каждого типа входа они задаются подсекциями
`[auth.<тип>.roles.<провайдер>]`: секция есть, провайдер подключён, роли всех
провайдеров складываются, исключение у любого из них отказ. Порог
`require_roles` действует на объединение.

| Провайдер | Кому доступен | Что делает |
|---|---|---|
| `local` | local, proxy | таблица `mapping` логин → роли и список `exclude` |
| `directory` | ldap | правила `samaccountname`, `member_of`, `dn` и `*_ex` по записи, которую вход получил своим bind'ом |
| `ldap` | kerberos, proxy | поиск записи под служебным bind'ом (`server`, `base_dn`, `bind_dn`, `bind_password`) и те же правила в `mapping`; kerberos ищет по UPN, proxy по sAMAccountName |
| `principal` | kerberos | правила `principal`, `sid` и `*_ex` по принципалу и SID из PAC |
| `header` | proxy | роли через запятую в заголовке `name` |

Профили: провайдер по ролям подключён у всех типов и настроек не имеет, он
выдаёт профили, чьи `[profiles.X].roles` пересекаются с ролями входа. У proxy
есть второй, `[auth.proxy.profiles.header]`: имена профилей через запятую в
заголовке `name` и, если задан `selected`, выбранный для новых чатов профиль в
отдельном заголовке. Имя, которого нет в `[profiles]`, отказ входа; выбранный
профиль обязан входить в выданный набор. Набор и выбор фиксируются в токене:
смена `[profiles.X].roles` действует после нового входа, у kerberos после
ближайшего обновления сессии.

Какой профиль получает новый чат, решается в одном порядке для чата и api:
выбор пользователя в интерфейсе, затем выбранный профиль из токена, затем
единственный выданный, затем профиль с `default = true`, если он выдан,
иначе отказ. Для copilot, где селектора профилей нет, работают второй и
четвёртый шаги.

Пример для всех четырёх типов:

```toml
[auth.local]
    type          = "local"
    users         = "${site.local_auth.users}"
    require_roles = true
    [auth.local.roles.local]
        mapping = "${site.local_auth.roles}"

[auth.ldap]
    type             = "ldap"
    server           = "${site.ldap_url}"
    base_dn          = "${site.ldap_base_dn}"
    bind_dn_template = '${site.ldap_netbios}\{username}'
    [auth.ldap.roles.directory]
        member_of = "${role.ldap.memberof}"
        dn        = "${role.ldap.dn}"

[auth.kerberos]
    type             = "kerberos"
    principal_format = "{username}@${site.krb_realm}"
    [auth.kerberos.roles.ldap]
        server        = "${site.ldap_url}"
        base_dn       = "${site.ldap_base_dn}"
        bind_dn       = "${site.ldap_bind_user}"
        bind_password = "${site.ldap_bind_password}"
        [auth.kerberos.roles.ldap.mapping]
            member_of = "${role.ldap.memberof}"
```

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

### Вход по доверенному заголовку (proxy)

Для сайтов и сервисов, которые сами знают пользователя и хотят открыть ему
boba без пароля и без раздачи `auth_secret`. Бэкенд партнёра называет логин
заголовком и подписывает запрос общим секретом, boba выпускает обычную
сессию. Работает и в chainlit, и в studio.

Секция в `config.toml` обоих приложений, плюс `"${auth.proxy}"` в списке
`[app].auth`:

```toml
[auth.proxy]
    type            = "proxy"
    path            = "/auth/proxy"
    secret          = "${site.proxy_secret}"
    max_skew_sec    = 60
    allowed_clients = ["10.20.30.40/32"]
    require_roles   = true
    [auth.proxy.headers]
        user      = "X-Remote-User"
        timestamp = "X-Boba-Timestamp"
        signature = "X-Boba-Signature"
    [auth.proxy.roles.local]
        mapping = { "portal-bot" = ["read"] }
    [auth.proxy.roles.ldap]
        server        = "${site.ldap_url}"
        base_dn       = "${site.ldap_base_dn}"
        bind_dn       = "${site.ldap_bind_user}"
        bind_password = "${site.ldap_bind_password}"
        [auth.proxy.roles.ldap.mapping]
            member_of = "${role.ldap.memberof}"
    [auth.proxy.roles.header]
        name = "X-Remote-Roles"
    [auth.proxy.profiles.header]
        name     = "X-Remote-Profiles"
        selected = "X-Remote-Profile"
```

- **`secret`.** Отдельный ключ HMAC, не `auth_secret`. Генерация:
  `openssl rand -hex 32`. Живёт у партнёра и в boba, ротируется без разлогина
  остальных пользователей.
- **`allowed_clients`.** Сети CIDR, откуда принимается вход. Пустой список
  выключает фильтр. Адрес берётся из `X-Forwarded-For` или `X-Real-IP`,
  которые выставляет nginx, поэтому порт приложения должен быть закрыт для
  всех, кроме nginx.
- **Роли и профили.** Провайдеры `roles.local`, `roles.ldap`, `roles.header`
  и `profiles.header` описаны выше. Каталог только даёт роли: логин, которого
  в нём нет, входит с остальными провайдерами. Любую секцию можно не
  указывать.
- **Заголовки.** Логин, метка времени в unix-секундах и подпись. Подпись
  это HMAC-SHA256 в hex от строки `login:timestamp:roles:profiles:profile`,
  где `roles`, `profiles` и `profile` это значения заголовков ролей, набора
  профилей и выбранного профиля как есть или пустые строки, если заголовок не
  настроен или не прислан. Запрос старше `max_skew_sec` отвергается.

Вызов:

```bash
TS=$(date +%s)
SIG=$(printf 'portal-bot:%s:ops:general,search:search' "$TS" | openssl dgst -sha256 -hmac "$PROXY_SECRET" | awk '{print $NF}')

curl -s -c cookies.txt -X POST "$B/auth/proxy" \
  -H "X-Remote-User: portal-bot" \
  -H "X-Remote-Roles: ops" \
  -H "X-Remote-Profiles: general,search" \
  -H "X-Remote-Profile: search" \
  -H "X-Boba-Timestamp: $TS" \
  -H "X-Boba-Signature: $SIG"
```

Ответ `204` и cookie `access_token`, дальше как у любого входа. В studio тот
же маршрут под api: `{prefix}/api/v1/auth/proxy`. На PHP подпись считается
одной строкой:
`hash_hmac('sha256', "$login:$ts:$roles:$profiles:$profile", $secret)`.

Отказы: `401` подпись, окно времени или заголовки, `403` адрес вне сетей,
исключение по логину или ни одной роли. Причина в теле ответа и в логе.

У такого входа нет kerberos-билета: инструменты, работающие в базе от имени
пользователя, откажут так же, как при парольном входе.

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
