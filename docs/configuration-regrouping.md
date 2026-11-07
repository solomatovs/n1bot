# Целевые конфигурации и данные MCP, AID, BOBA

Три отдельных сервиса: AID → BOBA по ACP/HTTP, AID → MCP по HTTP.
Ниже — целевые файлы конфигурации и схема каталогов AID.

## MCP

```sh
boba-mcp serve --config /etc/boba/mcp.toml \
   --plugin-config '/etc/boba/plugins/*.conf'
```

`--plugin-config` принимает файл или glob и допускает повторение. Загружаются
все выбранные файлы; для исключения из `*.conf` достаточно переименования.

### `mcp.toml`

```toml
# Расширяемый словарь путей в файловой системе
[paths]
   # Заранее подготовленные данные, readonly (админом кладуться в папку)
   runtime          = "${oc.env:MCP_RUNTIME}"
   # Создаваемые приложением данные, read/write (не удаляй без надобности)
   data             = "${oc.env:MCP_DATA}"
   sandbox          = "${paths.runtime}/sandbox"
   certs            = "${paths.runtime}/certs"
   workspace        = "${paths.data}/workspace"
   tool_logs        = "${paths.data}/tool-logs"
   kerberos_cache   = "${paths.data}/krb-cache"

[server]
   listen_host = "127.0.0.1"
   port        = 8650
   prefix_url  = "/mcp"
   public_url  = "https://loshara.com${server.prefix_url}"
   
   # Шифрование секретов в БД
   storage_key = "${oc.env:MCP_STORAGE_KEY}"
   # Подпись JWT и шифрование OAuth-состояния
   session_key = "${oc.env:MCP_SESSION_KEY}"
   
   [server.tls]
      cert_file = "${paths.certs}/cert.crt"
      key_file  = "${paths.certs}/private.key"
      ca_file   = "${paths.certs}/ca-chain.crt"

[database.postgres]
   host             = "loshara.com"
   port             = 5432
   dbname           = "${oc.env:MCP_DB}"
   schema           = "${oc.env:MCP_DB_SCHEMA}"
   application_name = "mcp"
   sslmode          = "verify-full"
   sslrootcert      = "${server.tls.ca_file}"
   pool             = { min_size = 1, max_size = 60, timeout = 2 }

   [database.postgres.auth]
      method   = "password"
      user     = "${oc.env:MCP_DB_USER}"
      password = "${oc.env:MCP_DB_PASSWORD}"

[auth]
   [auth.session]
      # Шифрование передаваемых Kerberos credentials
      credential_key   = "${oc.env:MCP_CREDENTIAL_KEY}"
      token_ttl_sec    = 3600
      max_lifetime_sec = 28800
      generation       = "mcp-1"

   # Системная авторизация: sys_provider, sys_role_source, sys_oauth_client.
   # Связи провайдеров с источниками — sys_provider_role_source.

[workspace]
   kind         = "image"
   # откуда взять шаблон для workspace
   template     = "${paths.sandbox}/workspace.ext4.gz"
   # куда скопировать шаблон workspace для конкретного пользователя 
   user_image   = "${paths.workspace}/{user_id}.ext4"
   # папка монтирования workspace для пользователя в процесс выполнения tools'а
   sandbox_path = "/workspace"

[tools]
   # нужно ли вести журналы выполнения tools
   journal = { enabled = true }

   [tools.launcher]
      # тип провайдера для tool launcher (бывает process вариант)
      provider    = "sandbox"
      # где расположена cgroup папка в которой можно работать mcp (rw доступ)
      cgroup_base = "${oc.env:MCP_CGROUP_BASE}"

[logging]
   loggers = { "uvicorn.access" = { level = "WARNING" } }
```

Каждый плагин имеет самостоятельный конфиг: свои пути, подключения и инференс.
Внешние значения берутся из env; настройки основного конфига не наследуются.

### `plugins/pg.conf`

```toml
max_rows  = 500
max_bytes = 1000000

[sandbox]
   program = "${oc.env:MCP_RUNTIME}/plugins/bin/db"
   rootfs  = "${oc.env:MCP_RUNTIME}/plugins/rootfs/base.ext4"

   [sandbox.limits]
      group_memory_bytes = 1073741824
```

### `plugins/bash.conf`

```toml
max_output_bytes = 65536
timeout_sec      = 3600

[sandbox]
   program   = "${oc.env:MCP_RUNTIME}/plugins/bin/shell"
   rootfs    = "${oc.env:MCP_RUNTIME}/plugins/rootfs/shell.ext4"
   workspace = true

   [sandbox.limits]
      group_memory_bytes = 1073741824
```

### `plugins/doc.conf`

```toml
text_encodings = ["utf-8", "cp1251"]

[ocr]
   provider   = "rapidocr"
   # указывается путь, который внутри sandbox
   models = "/var/cache/rapidocr"
   language   = "eslav"
   text_score = 0.5
   threads    = 2

[sandbox]
   program   = "${oc.env:MCP_RUNTIME}/plugins/bin/docs"
   rootfs    = "${oc.env:MCP_RUNTIME}/plugins/rootfs/base.ext4"
   mounts    = [
      "${oc.env:MCP_RUNTIME}/models/rapidocr:/var/cache/rapidocr",
   ]
   workspace = true

   [sandbox.limits]
      process_memory_bytes = 17179869184
      group_memory_bytes   = 4294967296
```

### `plugins/web.conf`

OCR извлекает текст из скачанного документа при `ocr_enabled=true`.

```toml
text_encodings = ["utf-8", "cp1251"]

[ocr]
   provider = "openai"
   # vision-model - условное имя, нужно заменить на доступную у провайдера модель с поддержкой изображений.
   model    = "vision-model"
   base_url = "https://${oc.env:WEB_INFERENCE_HOST}/v1"
   auth     = { method = "bearer", token = "${oc.env:WEB_INFERENCE_TOKEN}" }

[sandbox]
   program = "${oc.env:MCP_RUNTIME}/plugins/bin/docs"
   rootfs  = "${oc.env:MCP_RUNTIME}/plugins/rootfs/base.ext4"

   [sandbox.limits]
      process_memory_bytes = 4294967296
      group_memory_bytes   = 2147483648
```

### `plugins/kb.conf`

```toml
collection       = "kb_confluence"
tables           = { schema = "kb", sources_table = "kb_sources" }
max_result_chars = 1000000

[database.postgres]
   host             = "loshara.com"
   port             = 5432
   dbname           = "${oc.env:KB_DB}"
   schema           = "${oc.env:KB_SCHEMA}"
   application_name = "kb"
   sslmode          = "verify-full"
   sslrootcert      = "${oc.env:MCP_RUNTIME}/certs/ca-cert.crt"
   pool             = { min_size = 1, max_size = 4, timeout = 2 }

   [database.postgres.auth]
      method   = "password"
      user     = "${oc.env:KB_DB_USER}"
      password = "${oc.env:KB_DB_PASSWORD}"

[embedding]
   provider       = { kind = "fastembed", cache_dir = "/var/cache/fastembed" }
   model          = "intfloat/multilingual-e5-large"
   dim            = 1024
   batch_size     = 8
   progress_every = 1

[sandbox]
   program = "${oc.env:MCP_RUNTIME}/plugins/bin/docs"
   rootfs  = "${oc.env:MCP_RUNTIME}/plugins/rootfs/base.ext4"
   mounts  = [
      "${oc.env:MCP_RUNTIME}/models/fastembed:/var/cache/fastembed",
   ]

   [sandbox.limits]
      process_memory_bytes = 17179869184
      group_memory_bytes   = 8589934592
      group_cpu_percent    = 400
```

### `plugins/ingest.conf`

```toml
collection     = "kb_confluence"
tables         = { sources_table = "kb_sources" }
text_encodings = [
   "utf-8",
   "cp1251",
]
attachments    = [
   "application/pdf",
   "text/*",
   "image/*",
]
page_workers   = 4
table_shape    = { row_layout_max_columns = 4, row_layout_min_rows = 3 }

[database.postgres]
   host             = "loshara.com"
   port             = 5432
   dbname           = "${oc.env:INGEST_DB}"
   schema           = "${oc.env:INGEST_SCHEMA}"
   application_name = "ingest"
   sslmode          = "verify-full"
   sslrootcert      = "${oc.env:MCP_RUNTIME}/certs/ca-cert.crt"
   pool             = { min_size = 1, max_size = 4, timeout = 2 }

   [database.postgres.auth]
      method   = "password"
      user     = "${oc.env:INGEST_DB_USER}"
      password = "${oc.env:INGEST_DB_PASSWORD}"

[embedding]
   provider       = { kind = "fastembed", cache_dir = "/var/cache/fastembed" }
   model          = "intfloat/multilingual-e5-large"
   dim            = 1024
   batch_size     = 8
   progress_every = 1

[ocr]
   provider   = "rapidocr"
   models = "/var/cache/rapidocr"
   language   = "eslav"
   text_score = 0.5
   threads    = 2

[sandbox]
   program = "${oc.env:MCP_RUNTIME}/plugins/bin/docs"
   rootfs  = "${oc.env:MCP_RUNTIME}/plugins/rootfs/base.ext4"
   mounts  = [
      "${oc.env:MCP_RUNTIME}/models/fastembed:/var/cache/fastembed",
      "${oc.env:MCP_RUNTIME}/models/rapidocr:/var/cache/rapidocr",
   ]

   [sandbox.limits]
      process_memory_bytes = 17179869184
      group_memory_bytes   = 8589934592
      group_cpu_percent    = 400
```

## AID — `aid.toml`

```toml
[paths]
   # Заранее подготовленные данные, readonly (админом кладуться в папку)
   runtime           = "${oc.env:AID_RUNTIME}"
   # Создаваемые приложением данные, read/write (не удаляй без надобности)
   data              = "${oc.env:AID_DATA}"
   certs             = "${paths.runtime}/certs"
   chainlit_files    = "${paths.data}/chainlit-files"
   kerberos_cache    = "${paths.data}/krb-cache"

[server]
   listen_host = "127.0.0.1"
   port        = 8501
   prefix_url  = "/aid"
   public_url  = "https://loshara.com${server.prefix_url}"

   # Шифрование секретов в БД
   storage_key = "${oc.env:AID_STORAGE_KEY}"
   # Подпись JWT и шифрование OAuth-состояния
   session_key = "${oc.env:AID_SESSION_KEY}"
   
   [server.tls]
      cert_file = "${paths.certs}/cert.crt"
      key_file  = "${paths.certs}/private.key"
      ca_file   = "${paths.certs}/ca-chain.crt"

   # Отдельные настройки для MCP Apps
   [server.mcp_apps]
      # Браузер открывает в iframe: https://aid.loshara.com:8502
      port = 8502

[database.postgres]
   host             = "loshara.com"
   port             = 5432
   # Для отладки используй другую базу
   dbname           = "${oc.env:AID_DB}"
   schema           = "${oc.env:AID_DB_SCHEMA}"
   application_name = "aid"
   sslmode          = "verify-full"
   sslrootcert      = "${server.tls.ca_file}"
   pool             = { min_size = 1, max_size = 60, timeout = 2 }

   [database.postgres.auth]
      method   = "password"
      user     = "${oc.env:AID_DB_USER}"
      password = "${oc.env:AID_DB_PASSWORD}"

[auth]
   [auth.session]
      # Шифрование передаваемых Kerberos credentials
      credential_key   = "${oc.env:AID_CREDENTIAL_KEY}"
      token_ttl_sec    = 86400
      max_lifetime_sec = 604800
      generation       = "aid-1"
      cookie           = { name = "access_token", same_site = "lax" }

   # Системная авторизация: sys_provider, sys_role_source, sys_oauth_client.
   # Связи провайдеров с источниками — sys_provider_role_source.

[messaging]
   provider = "postgres"
   database = "${database.postgres}"
   node_id  = "aid-1"

[logging]
   loggers = { "uvicorn.access" = { level = "WARNING" } }
```

## BOBA — `boba.toml`

```toml
# Расширяемый словарь путей в файловой системе
[paths]
   # Заранее подготовленные данные, readonly (админом кладуться в папку)
   runtime        = "${oc.env:BOBA_RUNTIME}"
   # Создаваемые приложением данные, read/write (не удаляй без надобности)
   data           = "${oc.env:BOBA_DATA}"
   # Катало локальных моделей: /onnx-genai
   models         = "${paths.runtime}/models"
   kerberos_cache = "${paths.data}/krb-cache"

[server]
   listen_host = "127.0.0.1"
   port        = 8660
   prefix_url  = "/agent"
   public_url  = "https://loshara.com${server.prefix_url}"
   
   # Шифрование секретов в БД
   storage_key = "${oc.env:BOBA_STORAGE_KEY}"
   # Подпись JWT и шифрование OAuth-состояния
   session_key = "${oc.env:BOBA_SESSION_KEY}"
   
   [server.tls]
      cert_file = "${paths.certs}/cert.crt"
      key_file  = "${paths.certs}/private.key"
      ca_file   = "${paths.certs}/ca-chain.crt"

[database.postgres]
   host             = "loshara.com"
   port             = 5432
   dbname           = "${oc.env:BOBA_DB}"
   schema           = "${oc.env:BOBA_DB_SCHEMA}"
   application_name = "boba"
   sslmode          = "verify-full"
   sslrootcert      = "${server.tls.ca_file}"
   pool             = { min_size = 1, max_size = 60, timeout = 2 }

   [database.postgres.auth]
      method   = "password"
      user     = "${oc.env:BOBA_DB_USER}"
      password = "${oc.env:BOBA_DB_PASSWORD}"

[auth]
   [auth.session]
      # Шифрование передаваемых Kerberos credentials
      credential_key   = "${oc.env:BOBA_CREDENTIAL_KEY}"
      token_ttl_sec    = 3600
      max_lifetime_sec = 28800
      generation       = "boba-1"

   # Системная авторизация: sys_provider, sys_role_source, sys_oauth_client.
   # Связи провайдеров с источниками — sys_provider_role_source.

[logging]
   loggers = { "uvicorn.access" = { level = "WARNING" } }
```

## Назначение настроек

| Поле / секция | Назначение |
| --- | --- |
| `server` | Адрес прослушивания, порт, URL-префикс, публичный URL и TLS |
| AID `server.mcp_apps.port` | Отдельный порт MCP Apps, отличающийся от порта чата; host и TLS общие с AID |
| `paths` | Расширяемый словарь путей; произвольные имена доступны через `${paths.<имя>}` |
| `paths.runtime` | Заранее подготовленные ресурсы приложения, только чтение |
| `paths.data` | Создаваемые приложением данные, включая постоянные файлы и рабочие кэши |
| `paths.sandbox` / `certs` | Имена для интерполяции путей к образам и сертификатам в примерах |
| `paths.workspace` / `tool_logs` | Постоянные пользовательские образы / журналы инструментов |
| AID `paths.upload_cache` | Временные файлы загрузок Chainlit |
| `paths.kerberos_cache` | Рабочие ccache; keytab и `krb5.conf` размещаются в `krb/` рядом с основным конфигом |
| BOBA `paths.models` | Подготовленные ONNX-модели: подкаталог по имени модели. В AID пути к весам не хранятся |
| `database.postgres` | Подключение, авторизация и пул; база `aid`, для отладки `aid_ss`. `schema` необязательна в модели подключения; хранилище задаёт нужную схему |
| Плагин `tables.schema` | Схема набора таблиц; по умолчанию `database.postgres.schema`. В kb поиск использует `ix`, таблицы чанков — `kb` |
| `workspace` | `template` — шаблон; `user_image` — постоянный образ пользователя; `sandbox_path` — точка монтирования для инструмента |
| `tools.launcher` | Способ запуска и общая ветка `cgroup_base` экземпляра MCP |
| Плагин `sandbox` | Программа, rootfs, mounts, подключение workspace и лимиты конкретного плагина |
| Плагин `sandbox.mounts` | `путь в MCP:путь в песочнице`, только чтение. Пути OCR/эмбеддингов указывают на назначение mount |
| Плагин `ocr` / `embedding` | HTTP-провайдер либо локальный движок с подготовленными моделями. У kb/ingest модель и размерность эмбеддингов совпадают |
| MCP tools | Инструменты предоставляются манифестами плагинов; выполнение проверяет `can_use` на capability `tool.<plugin>.execute` |
| AID `messaging` | `database` — подключение PostgreSQL; `node_id` — уникальное имя узла |
| `logging` | Переопределения стандартного dictConfig |

Пути относятся к файловой системе процесса сервиса; `sandbox_path` и назначения
mounts — к песочнице инструмента. Само объявление имени в `paths` каталог не создаёт.

### Авторизация

Провайдеры входа, источники ролей и их credentials хранятся в БД.
В TOML остаются подключение к самой БД, корневые ключи и `auth.session`
с параметрами срока и cookie сессии.

### Секретные ключи

| Настройка | Назначение |
| --- | --- |
| `server.storage_key` | Шифрование секретов `app_auth`, содержимого `app_session` и столбцов `credential` в системных таблицах |
| `server.session_key` | Подпись JWT и шифрование OAuth-состояния; общий у реплик службы |
| `auth.session.credential_key` | Шифрование передаваемых Kerberos credentials; общий у обменивающихся ими служб |

Корневые ключи остаются вне БД. Ключи и shared secrets: 32 случайных байта,
URL-safe Base64. Шифрование — Fernet, подписи — HS256/HMAC-SHA256,
внутренние ключи назначения выводятся через HKDF-SHA256.

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```


## DDL таблиц управления приложением

В приложении используются таблицы:
- sys_provider - провайдеры атворизации в приложении
- sys_role_source - источник ролей, которые назначаются после авторизации
- sys_provider_role_source - таблица сшивает провайдеры авторизации и источники ролей
- sys_oauth_client - доверенные приложения oauth которые проходят через proxy авторизацию

При старте приложение читает активные настройки, расшифровывает `credential`
и собирает провайдеры в памяти. Далее приложение не читает таблицы, все необходимо храниться в памяти до момента когда придет сигнал SIGHUP для обновления. В этот момент системные таблицы перечитываются и память обновляется.


```sql
-- здесь я использую схему aid для примера
-- те же таблицы будут в приложениях boba и mcp
-- так как система основа у них общая
create schema aid;

-- провайдеры авторизации
create table aid.sys_provider (
   name        varchar     primary key,
   kind        varchar     not null, -- local, ldap, kerberos, proxy
   payload     jsonb       not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
   credential  jsonb       not null default '{}'::jsonb check (jsonb_typeof(credential) = 'object'),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now()
);
```

Примеры строк представлены в виде json для удобства. Обрати внимание, что секретная часть должна быть зашифрована, механизм шифрования уже разработан в коде и его надо использовать для секретной части. Для удобства в примерах нет шифрования.

Так мог бы выглядеть `payload`, `credential` для приложения:

```json
{
   "name": "local_users",
   "kind": "local",
   "payload": {},
   "credential": {
      "users": {
         "first": "$argon2id$..."
      }
   }
}
```

```json
{
   "name": "ldap",
   "kind": "ldap",
   "payload": {
      "connection": {
         "server": "ldaps://dc.loshara.com:636",
         "base_dn": "DC=example,DC=org"
      },
      "bind_dn_template": "{username}@EXAMPLE.ORG"
   },
   "credential": {}
}
```

Keytab здесь указан как файл на файловой системе.
```json
{
   "name": "kerberos",
   "kind": "kerberos",
   "payload": {
      "spn": "HTTP/mcp.example.org@EXAMPLE.ORG",
      "principal_format": "{username}@EXAMPLE.ORG"
   },
   "credential": {
      "keytab": "krb/mcp.keytab"
   }
}
```

```json
{
   "name": "proxy",
   "kind": "proxy",
   "payload": {
      "path": "/auth/proxy",
      "allowed_clients": ["127.0.0.1/32", "10.0.0.0/8"],
      "headers": {
         "user": "X-Remote-User",
         "timestamp": "X-Boba-Timestamp",
         "signature": "X-Boba-Signature"
      },
      "profiles": {
         "header": "X-Remote-Profiles",
         "selected_header": "X-Selected-Profile"
      }
   },
   "credential": {
      "secret": "..."
   }
}
```


```sql
-- источники ролей со своими правилами и параметрами подключения
create table aid.sys_role_source (
   name        varchar     primary key,
   kind        varchar     not null, -- local, ldap, principal, proxy
   payload     jsonb       not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
   credential  jsonb       not null default '{}'::jsonb check (jsonb_typeof(credential) = 'object'),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now()
);
```

Примеры `rules`:

```json
{
   "name": "local",
   "kind": "local",
   "payload": {
      "rules": [
         {"by": "login", "value": "admin", "roles": ["admin"]},
         {"by": "login", "value": "reader", "roles": ["read"]},
         {"by": "login", "value": "blocked", "deny": true}
      ]
   },
   "credential": {}
}
```

```json
{
   "name": "ldap",
   "kind": "ldap",
   "payload": {
      "connection": {
         "server": "ldaps://dc.loshara.com:636",
         "base_dn": "DC=example,DC=org"
      },
      "rules": [
         {"by": "group", "value": "CN=Readers,OU=Groups,DC=example,DC=org", "roles": ["read"]},
         {"by": "login", "value": "alice", "roles": ["read"]},
         {"by": "dn", "value": "CN=Admin,OU=People,DC=example,DC=org", "roles": ["admin"]},
         {"by": "group", "value": "CN=Blocked,OU=Groups,DC=example,DC=org", "deny": true}
      ]
   },
   "credential": {
      "connection": {
         "bind": {
            "method": "simple",
            "user": "CN=lookup,OU=Services,DC=example,DC=org",
            "password": "..."
         }
      }
   }
}
```

```json
{
   "name": "kerberos",
   "kind": "principal",
   "payload": {
      "rules": [
         {"by": "principal", "value": "alice@EXAMPLE.ORG", "roles": ["read"]},
         {"by": "sid", "value": "S-1-5-21-111-222-333-1001", "roles": ["admin"]},
         {"by": "principal", "value": "blocked@EXAMPLE.ORG", "deny": true},
         {"by": "sid", "value": "S-1-5-21-111-222-333-1002", "deny": true}
      ]
   },
   "credential": {}
}
```

```json
{
   "name": "proxy",
   "kind": "proxy",
   "payload": {
      "header": "X-Remote-Roles"
   },
   "credential": {}
}
```

Для анонимного LDAP-поиска в `credential`:
`{"connection":{"bind":{"method":"anonymous"}}}`.
Правила назначают роли по имени из `app_identity(kind='role')`;
совпадение `deny=true` запрещает вход.
Proxy-источник получает роли из проверенного утверждения.

```sql
-- таблица объединяет провайдеры авторизации и источники ролей, которые присвоены этим провайдерам авторизации
create table aid.sys_provider_role_source (
   provider_name     varchar     not null references aid.sys_provider (name),
   role_source_name  varchar     not null references aid.sys_role_source (name),
   enable            boolean     not null default true,
   created_at        timestamptz not null default now(),
   updated_at        timestamptz not null default now(),

   primary key (provider_name, role_source_name)
);
```

Примеры строк связи из исходного конфига:

| `provider_name` | `role_source_name` |
| --------------- | ------------------ |
| `local_users`   | `local`            |
| `ldap`          | `ldap`             |
| `kerberos`      | `kerberos`         |
| `kerberos`      | `ldap`             |
| `proxy`         | `proxy`            |
| `proxy`         | `ldap`             |
| `proxy`         | `local`            |

```sql
-- одно доверенное приложение из auth.oauth.clients
create table aid.sys_oauth_client (
   client_id   varchar     primary key,
   credential  jsonb       not null check (jsonb_typeof(credential) = 'object'),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now()
);
```

Пример отдельной строки OAuth-клиента:

```json
{
   "client_id": "aid",
   "credential": {
      "secret": "..."
   }
}
```

Секрет OAuth-клиента проверяет приложение, а секрет proxy-провайдера —
его утверждение о пользователе. В БД непустой `credential` шифруется целиком:
`{"ciphertext":"<fernet>"}`.

Подтверждённый логин сопоставляется с `app_identity(kind='user')`.
Пользователи, членство и прикладные гранты остаются в `app_`.

## DDL таблиц приложения (общие таблицы для приложений aid, boba, mcp)

Префикс `app_` используется для общих таблиц приложения.
Ниже используется схема `aid`, но в приложениях boba, mcp будет использоваться собственная.

Система работает через общую таблицу сквозных id которые генерируются в таблице app_ident.
Все остальные таблиц получают идентификатор который ссылкается на эту таблицу.
Это позволяет выдавать гранты на любой объект в приложении.
Для управления консистентным состоянием используются транзакции в postgres (это очень важно!)


```sql
-- сквозная нумерация прикладных объектов
create table aid.app_ident (
   id    bigserial not null,
   -- название конкретного типа идентификатора
   kind  varchar   not null check (length(kind) > 0),

   primary key (id)
);

-- таблица пользователей, ролей, команд и внутренних служб приложения.
-- таблица выполнена в виде набора идентификаторов с типом и именем
-- членство задаётся через app_identity_member
-- например для группировки по ролям или по командам
create table aid.app_identity (
   id          bigint      not null references aid.app_ident (id),
   -- имя идентификатора: adm, read, write, n1 и другие
   name        varchar     not null check (length(name) > 0),
   -- service используется только внутренним обработчиком, не для входа пользователя
   kind        varchar     not null check (kind in ('user', 'role', 'team', 'service')),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),
   primary key (id)
);
create unique index uk_app_identity_kind_name on aid.app_identity(kind, name);

-- таблица содержит вложение identity друг в друга
-- роли, команды, пользователи
create table aid.app_identity_member (
   id             bigint      not null references aid.app_ident (id),
   -- участник: пользователь, служба, роль или команда
   identity_id    bigint      not null references aid.app_identity (id),
   -- родительская identity; допустимость связи и отсутствие циклов проверяются при записи
   member_id      bigint      not null references aid.app_identity (id),
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),
   check (member_id <> identity_id),
   
   primary key (id)
);
create unique index uk_app_identity_member on aid.app_identity_member(identity_id, member_id);


-- самостоятельные возможности: вход, создание объектов, выполнение инструментов
-- app_ident.kind = 'app_capability'; scope_id и связи с sys_* не требуются
create table aid.app_capability (
   id          bigint      not null references aid.app_ident (id),
   -- стабильное имя операции, известное приложению
   name        varchar     not null check (length(name) > 0),
   description varchar     not null check (length(description) > 0),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id),
   unique (name)
);

-- шаблон первоначальных грантов; после применения связь с ним не сохраняется
create table aid.app_grant_default (
   id          bigint      not null references aid.app_ident (id),
   -- user.created, owner.skill, owner.connection и другие
   name        varchar     not null check (length(name) > 0),
   description varchar     not null check (length(description) > 0),
   -- массив правил: kind, target и name для target = capability
   payload     jsonb       not null default '[]'::jsonb
                           check (jsonb_typeof(payload) = 'array'),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id),
   unique (name)
);

-- obj_id разрешено действие kind на sbj_id
-- все строки равноправны: происхождение и связь с defaults не сохраняются
create table aid.app_grant (
   id          bigint      not null references aid.app_ident (id),
   -- получатель: пользователь, роль, команда или служба
   obj_id      bigint      not null references aid.app_identity (id),
   -- конкретный ресурс или capability
   sbj_id      bigint      not null references aid.app_ident (id),
   kind        varchar     not null check (kind in (
      'can_view', 'can_use', 'can_edit', 'can_publish', 'can_delete',
      'can_grant', 'can_manage_members', 'can_bind', 'can_revoke'
   )),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id),
   unique (obj_id, sbj_id, kind)
);
create index idx_app_grant_target on aid.app_grant(sbj_id, kind, obj_id);
```

### Действия и единая проверка грантов

| `kind` | Назначение |
| --- | --- |
| `can_view` | Просмотр объекта; секреты `app_auth` / `app_session` не выдаются |
| `can_use` | Использование ресурса или выполнение capability |
| `can_edit` | Изменение обычных полей; добавление версии промпта/скила |
| `can_publish` | Переключение актуальной `revision`; доступ другим пользователям не выдаётся |
| `can_delete` | Удаление или вывод из использования; сохранённые версии не переписываются |
| `can_grant` | Выдача/отзыв имеющихся у выдающего прав на этот объект |
| `can_manage_members` | Изменение состава конкретной роли/команды |
| `can_bind` | Разрешение привязать credentials к подключению |
| `can_revoke` | Прекращение исходящей сессии |

Операции выполняются в `READ COMMITTED`. Исполнителя и требуемые права определяет
сервер. В транзакционных примерах сервис выполняет запросы на одном соединении;
при отказе или ошибке — `ROLLBACK`, дальнейшие шаги не выполняются.

Проверка наследования и грантов читает один снимок SQL-оператора. Начавшаяся операция
может завершиться после параллельного отзыва; следующий оператор увидит отзыв.
Результат проверки не переносится в другую транзакцию и не кэшируется между действиями.

`enable = false` отключает строку гранта, но не отменяет разрешение другой identity.
Наличие owner/admin не даёт обхода; `can_grant` передаётся отдельной явной строкой.
Проверка выполняется при каждом действии, включая использование уже созданной сессии.

Операция также проверяет допустимость действия для типа объекта, его состояние и связи:
`can_use` credentials применяется только к разрешённому подключению; изменение адреса
требует повторной проверки привязки. Добавление ресурса к чужому thread/провайдеру
требует права изменять этот контейнер. Эти права проверяются тем же сервисом.
`can_edit` не меняет владельца, тип объекта, гранты или членство.

В примерах: identity `10 = alice`, `11 = bob`, `20 = team/dev`, `30 = role/read`,
`40 = team/other`, `50 = service/auth-service`; подключения — `100`, `101`.
Примеры независимы, кроме явно указанного повторного вызова.

#### Общая функция: получить иерархию identity и его родителей

```sql
create or replace function aid.app_principals(identity_id bigint)
returns table(id bigint)
language sql stable
as $$
   with recursive principals(id) as (
      select
         i.id
      from
         aid.app_identity as i
      where
         i.id = identity_id
      union
      select
         m.member_id
      from
         principals as p
         join aid.app_identity_member as m on m.identity_id = p.id
   )
   select id from principals;
$$;
```

Пример данных:

| `app_identity_member.identity_id` | `member_id` |
| --- | --- |
| 10 | 20 |
| 20 | 30 |

`app_principals(10)` возвращает датасет `{10, 20, 30}`. Для отсутствующей identity — пустой датасет

#### Общая функция: проверить все пары «объект — действие»

```sql
create or replace function aid.app_has_permissions(
   identity_id bigint,
   targets bigint[],
   actions varchar[]
)
returns boolean
language sql stable
as $$
   with required as (
      -- Массивы сопоставляются по позиции; повторная пара проверяется один раз.
      select distinct
         r.id,
         r.kind
      from
         unnest(targets, actions) as r(id, kind)
   ), available as (
      -- Обход членства и чтение грантов выполняются в одном снимке.
      -- Два отдельных запроса могли бы совместить старое членство с новым грантом.
      select distinct
         g.sbj_id as id,
         g.kind
      from
         aid.app_principals(identity_id) as p
         join aid.app_grant as g on g.obj_id = p.id
         left join aid.app_capability as c on c.id = g.sbj_id
      where
         g.enable
         and g.sbj_id = any(targets)
         -- NULL означает обычный ресурс, а не выключенную capability.
         and (g.kind <> 'can_use' or c.enable is distinct from false)
   )
   select
      -- Пустые требования запрещены; каждая точная пара должна найти совпадение.
      -- NULL, неизвестный объект/действие и массивы разной длины дают отказ.
      count(*) > 0 and count(*) = count(a.id)
   from
      required as r
      left join available as a on a.id = r.id and a.kind = r.kind;
$$;
```

Пример данных: членство `10 → 20 → 30`; у `30` включён `can_use` на `100`,
у `10` включены `can_edit` на `100` и `can_use` на выключенную capability `203`.

| Вызов для `identity_id = 10` | Результат |
| --- | --- |
| `targets = {100}`, `actions = {can_use}` | `true`: наследование от `30`, строка capability для `100` не нужна |
| `targets = {100,100}`, `actions = {can_use,can_edit}` | `true`: нужны обе пары |
| `targets = {100}`, `actions = {can_publish}` | `false`: другое право не заменяет требуемое |
| `targets = {203}`, `actions = {can_use}` | `false`: capability выключена |
| `targets = {}`, `actions = {}` | `false` |

#### Общая функция: проверить получателя при выдаче гранта

```sql
create or replace function aid.app_can_grant_to(identity_id bigint, recipient_id bigint)
returns boolean
language sql stable
as $$
   select
      count(*) > 0
   from
      aid.app_identity as r
   where
      r.id = recipient_id
      and (
         -- Это политика выдачи; рекурсивный обход identity от kind не зависит.
         r.kind in ('user', 'service')
         or r.id in (select id from aid.app_principals(identity_id))
      );
$$;
```

Пример данных: `10 → 20 → 30`, `11` — пользователь, `40` — другая команда.
Для исполнителя `10` получатели `11`, `20`, `30` допустимы; `40` и отсутствующая
identity — нет. Права на сам объект эта функция не проверяет.

#### UC1. Проверить доступ к одному или нескольким ресурсам

```sql
-- $1 — исполнитель; $2 — bigint[] объектов; $3 — varchar[] действий.
-- Например, вызов MCP требует can_use на сервер, connection и credentials.
select aid.app_has_permissions($1, $2, $3) as allowed;
```

Пример данных: у `10` есть `can_use` на MCP `110`, connection `100` и credentials `120`.
`$2 = {110,100,120}`, `$3 = {can_use,can_use,can_use}` → `allowed = true`.
Отсутствие любого из трёх прав → `false`. Для изменения ресурса проверка включается
в сам изменяющий оператор, как в UC2.

#### UC2. Прочитать или изменить объект с проверкой прав

```sql
-- $1 — исполнитель; $2 — connection_id.
-- Возвращается только разрешённая строка; секреты app_auth этим запросом не читаются.
select
   c.id,
   c.name,
   c.kind,
   c.payload
from
   aid.app_connection as c
where
   c.id = $2
   and aid.app_has_permissions($1, array[$2], array['can_view']::varchar[]);
```

Пример данных: подключение `100` имеет `name = 'wiki'`, `payload = {"host":"old.example"}`.
При `can_view` для исполнителя `10` возвращается эта строка; без права — пустой результат.

```sql
-- $1 — исполнитель; $2 — connection_id; $3 — новый payload.
-- Проверка и UPDATE — один оператор; отдельного SELECT перед записью нет.
-- UPDATE сам блокирует изменяемую строку.
update
   aid.app_connection
set
   payload = $3,
   updated_at = now()
where
   id = $2
   and aid.app_has_permissions($1, array[$2], array['can_edit']::varchar[])
returning
   id;
```

Пример данных: `100.payload = {"host":"old.example"}`, у `10` есть `can_edit` на `100`.
`$3 = {"host":"new.example"}` → новая нагрузка и `id = 100`.
При отсутствии права возвращается ноль строк, исходная нагрузка сохраняется.

#### UC3. Выдать или повторно включить одно право

```sql
-- $1 — исполнитель; $2 — получатель; $3 — объект; $4 — право.
with permitted as materialized (
   -- Одновременно нужны can_grant и само передаваемое право.
   select
      aid.app_has_permissions(
         $1, array[$3, $3], array['can_grant', $4]::varchar[]
      ) and aid.app_can_grant_to($1, $2) as allowed
), saved as (
   insert into aid.app_grant as g (id, obj_id, sbj_id, kind)
   select
      nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
      $2,
      $3,
      $4
   from
      permitted as p
      left join aid.app_grant as current_grant
         on current_grant.obj_id = $2
         and current_grant.sbj_id = $3
         and current_grant.kind = $4
   where
      p.allowed
      -- Активный грант не трогаем: повторный запрос не создаёт очередь UPDATE.
      and (current_grant.id is null or not current_grant.enable)
   -- Два запроса могли одновременно не увидеть строку: UNIQUE разрешает гонку.
   on conflict (obj_id, sbj_id, kind) do update
      set enable = true,
          updated_at = now()
      where not g.enable
   returning id
), registered as (
   -- Общая последовательность сохранена. Регистрируем только фактический ID.
   -- При конфликте не остаётся лишнего app_ident; FK проверяются к концу оператора.
   insert into aid.app_ident (id, kind)
   select id, 'app_grant' from saved
   on conflict (id) do nothing
)
select
   allowed,
   (select count(*) from saved) > 0 as changed
from
   permitted;
```

Пример данных: у `10` есть `can_grant` и `can_use` на `100`; гранта `(11,100,can_use)` нет.

| Вызов `$1=10, $2=11, $3=100, $4=can_use` | Результат | Данные после запроса |
| --- | --- | --- |
| Первый | `allowed=true, changed=true` | Одна строка `(11,100,can_use,true)` и её `app_ident` |
| Повторный | `allowed=true, changed=false` | Строка и её ID не меняются |
| После отключения строки | `allowed=true, changed=true` | Та же строка включена, прежний ID сохранён |
| У исполнителя нет `can_grant` | `allowed=false, changed=false` | Ничего не записано |

#### UC4. Отозвать прямой грант

```sql
-- $1 — исполнитель; $2 — получатель; $3 — объект; $4 — право.
update
   aid.app_grant
set
   enable = false,
   updated_at = now()
where
   obj_id = $2
   and sbj_id = $3
   and kind = $4
   and enable
   and aid.app_has_permissions(
      $1, array[$3, $3], array['can_grant', $4]::varchar[]
   )
-- При отзыве принадлежность исполнителя к группе-получателю не требуется.
returning
   id;
```

Пример данных: грант `9001 = (11,100,can_use,true)` → `enable=false`, результат `id=9001`.
Повторный отзыв возвращает ноль строк. Если `11` получает `can_use` через роль,
это наследуемое право сохраняется. Параллельная первая выдача ещё отсутствующего
гранта может завершиться после отзыва и оставить его включённым.

#### UC5. Выдать набор прав целиком

```sql
-- $1 — исполнитель; $2 — JSON-массив записей {obj_id, sbj_id, kind}.
begin;

-- Сначала защищаем ВСЕ получатели и объекты от удаления, в порядке их общих ID.
-- KEY SHARE совместим между выдачами; удаление использует FOR UPDATE (UC9).
-- Если вернулись не все ID из набора — ROLLBACK, пакет не записываем.
with requested as (
   select * from jsonb_to_recordset($2) as r(obj_id bigint, sbj_id bigint, kind varchar)
), referenced as (
   select obj_id as id from requested
   union
   select sbj_id from requested
)
select
   i.id
from
   aid.app_ident as i
   join referenced as r on r.id = i.id
order by
   i.id
for key share of i;

with requested as materialized (
   -- Одинаковый ключ нельзя изменять дважды одним INSERT ... ON CONFLICT.
   select distinct obj_id, sbj_id, kind
   from jsonb_to_recordset($2) as r(obj_id bigint, sbj_id bigint, kind varchar)
), needed as (
   select sbj_id, kind from requested
   union
   select sbj_id, 'can_grant' from requested
), permitted as materialized (
   -- Проверяем весь пакет до записи: один отказ отменяет все его элементы.
   select
      (select aid.app_has_permissions($1, array_agg(sbj_id), array_agg(kind)) from needed)
      and coalesce(
         (select bool_and(aid.app_can_grant_to($1, obj_id)) from requested), false
      ) as allowed
), saved as (
   insert into aid.app_grant as g (id, obj_id, sbj_id, kind)
   select
      nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
      r.obj_id,
      r.sbj_id,
      r.kind
   from
      requested as r
      cross join permitted as p
      left join aid.app_grant as old
         on old.obj_id = r.obj_id and old.sbj_id = r.sbj_id and old.kind = r.kind
   where
      p.allowed
      and (old.id is null or not old.enable)
   -- Все пакетные записи используют один порядок ключей, независимо от JSON.
   order by r.obj_id, r.sbj_id, r.kind
   on conflict (obj_id, sbj_id, kind) do update
      set enable = true, updated_at = now()
      where not g.enable
   returning id
), registered as (
   insert into aid.app_ident (id, kind)
   select id, 'app_grant' from saved order by id
   on conflict (id) do nothing
)
select
   coalesce(allowed, false) as allowed,
   (select count(*) from saved) as changed
from permitted;

-- allowed=false: ROLLBACK; allowed=true: COMMIT.
commit;
```

Пример данных: у `10` есть `can_grant` и `can_use` на `100` и `101`; `10 → 20`.
Исходные прямые гранты для получателей отсутствуют.

```json
[
   {"obj_id":11, "sbj_id":100, "kind":"can_use"},
   {"obj_id":20, "sbj_id":101, "kind":"can_use"},
   {"obj_id":11, "sbj_id":100, "kind":"can_use"}
]
```

Результат: `allowed=true, changed=2`; дубликат не создаёт третью строку.
Если одно из прав исполнителю недоступно — `allowed=false, changed=0`.

#### UC6. Добавить членство без цикла

```sql
-- $1 — исполнитель; $2 — участник; $3 — родитель.
-- $4 — bigint[]: найденный сервером набор блокируемых identity, не ввод клиента.
begin;

-- 1. Узнать родителей $3 и добавить изменяемый узел $2. Результат сохранить в $4.
select id from aid.app_principals($3)
union
select $2;

-- 2. Блокировать все найденные identity в одинаковом порядке.
-- Если вернулись не все запрошенные строки — ROLLBACK.
select id
from aid.app_identity
where id = any($4)
order by id
for no key update;

-- 3. После ожидания блокировок прочитать граф НОВЫМ оператором.
-- acyclic=false: отказ. closure_locked=false: ROLLBACK и повторить с шага 1.
select
   count(*) > 0 and count(*) filter (where id = $2) = 0 as acyclic,
   count(*) filter (where not (id = any($4))) = 0 as closure_locked
from aid.app_principals($3);

-- 4. Источники всех прочитанных рёбер заблокированы; граф до записи не изменится.
with permitted as materialized (
   select aid.app_has_permissions(
      $1, array[$3], array['can_manage_members']::varchar[]
   ) as allowed
), allocated as (
   insert into aid.app_ident (kind)
   select 'app_identity_member'
   where
      (select allowed from permitted)
      -- Исходная identity заблокирована: конкурентное добавление не пройдёт сюда.
      and (select count(*) from aid.app_identity_member
           where identity_id = $2 and member_id = $3) = 0
   returning id
), inserted as (
   insert into aid.app_identity_member (id, identity_id, member_id)
   select id, $2, $3 from allocated
   returning id
)
select
   allowed,
   (select count(*) from inserted) > 0 as changed
from permitted;

-- allowed=false: ROLLBACK; иначе COMMIT, в том числе при уже существующей связи.
commit;
```

Пример данных: есть `20 → 30`, исполнитель `50` имеет `can_manage_members` на `20`.

| Добавление | Найденный набор блокировок | Результат |
| --- | --- | --- |
| `11 → 20` | `{11,20,30}` | `acyclic=true, closure_locked=true`; связь создаётся |
| Повторное `11 → 20` | `{11,20,30}` | `allowed=true, changed=false` |
| `30 → 20` | `{20,30}` | `acyclic=false`: получился бы цикл `30 → 20 → 30` |
| После первого чтения появилась `30 → 40` | Старый набор `{11,20,30}` | `closure_locked=false`: повторить с набором `{11,20,30,40}` |

Все изменения членства соблюдают этот порядок. Для нескольких рёбер сначала собирается
и блокируется общий набор identity; новые блокировки не добираются после первых записей.

#### UC7. Удалить членство

```sql
-- $1 — исполнитель; $2 — участник; $3 — родитель.
begin;

-- Этот же узел блокирует добавление связи и проверка циклов в UC6.
select id from aid.app_identity where id = $2 for no key update;

with removed as (
   delete from aid.app_identity_member
   where
      identity_id = $2
      and member_id = $3
      and aid.app_has_permissions(
         $1, array[$3], array['can_manage_members']::varchar[]
      )
   returning id
)
-- Удаляем общий ID только для действительно удалённой связи.
-- Существующие сторонние FK не обходятся: при конфликте откатывается операция.
delete from aid.app_ident as i
using removed as r
where i.id = r.id
returning i.id;

commit;
```

Пример данных: связь `9100 = (11,20)` и `can_manage_members` у `50` на `20`.
Удаляются `app_identity_member.id=9100` и `app_ident.id=9100`; следующий запрос
пользователя `11` больше не наследует права через эту связь. Повтор — ноль строк.

#### UC8. Добавить версию промпта и при необходимости опубликовать

```sql
-- $1 — исполнитель; $2 — prompt_id; $3 — description; $4 — content.
begin;

-- Блокируется только этот промпт. Два автора не выделят одинаковую новую revision.
select id from aid.prompt where id = $2 for no key update;
-- Если строка не найдена — ROLLBACK.

insert into aid.prompt_revision (
   prompt_id, revision, description, content, created_by
)
select
   $2,
   -- Текущая prompt.revision могла быть переключена на старую версию.
   coalesce(max(revision), 0) + 1,
   $3,
   $4,
   $1
from aid.prompt_revision
where prompt_id = $2
having aid.app_has_permissions($1, array[$2], array['can_edit']::varchar[])
returning revision;

-- Ноль строк: ROLLBACK. Сохранить новую версию без публикации: COMMIT.
-- Для одновременной публикации выполнить следующий запрос ДО COMMIT этой транзакции.
commit;
```

Пример данных: у `1000` есть версии `{1,2,3}`, но `prompt.revision=1`.
Следующая запись получает `revision=4`; указатель остаётся `1`. Конкурентный автор
после ожидания блокировки создаст `5`, а не вторую `4`. Для `skill_revision` действует тот же порядок.

```sql
-- $1 — исполнитель; $2 — prompt_id; $3 — публикуемая revision.
-- Выполняется отдельно либо внутри транзакции добавления версии выше.
update aid.prompt
set revision = $3, updated_at = now()
where
   id = $2
   and aid.app_has_permissions($1, array[$2], array['can_publish']::varchar[])
-- Составной FK (id, revision) запрещает выбрать чужую или отсутствующую версию.
-- Ноль строк при совместном создании и публикации: сервис делает ROLLBACK.
returning id, revision;
```

Пример данных: `prompt(1000).revision=1`, версия `4` существует, у `10` есть `can_publish`.
Результат — `(1000,4)`. Без права строка не меняется; для отсутствующей версии возникает
ошибка FK. При создании и публикации в одной транзакции отказ публикации откатывает обе операции.

#### UC9. Удалить подключение и его гранты

```sql
-- $1 — исполнитель; $2 — connection_id.
begin;

-- Запрещаем появление новых FK-ссылок на удаляемый объект.
-- Для нескольких удалений весь набор блокируется сразу, ORDER BY id FOR UPDATE.
select id from aid.app_ident where id = $2 for update;

with removed_object as (
   delete from aid.app_connection
   where
      id = $2
      and aid.app_has_permissions($1, array[$2], array['can_delete']::varchar[])
   returning id
), removed_grants as (
   delete from aid.app_grant as g
   using removed_object as o
   where g.sbj_id = o.id
   returning g.id
), removed_grant_ids as (
   delete from aid.app_ident as i
   using removed_grants as g
   where i.id = g.id
)
delete from aid.app_ident as i
using removed_object as o
where i.id = o.id
returning i.id;

-- FK от app_session и других зависимых объектов сохраняются.
-- Если зависимость мешает удалению, откатывается и удаление грантов.
commit;
```

Пример данных: подключение `100`, его гранты `9001`, `9002`, внешних зависимостей нет.
Удаляются подключение, оба гранта и три строки `app_ident`; возвращается `100`.
Если `app_session.connection_id=100`, удаление целиком завершается ошибкой FK.

### Начальная выдача прав

`user.created` применяется к новому пользователю; `owner.*` — к создателю нового объекта.
Объекты создаются с `owner_id` текущего пользователя; предоставление доступа другим
identity выполняется отдельно. Для технической сессии получатель — её `identity_id`;
при `identity_id = NULL` персональные гранты не создаются.

Пример `user.created.payload` (вход через LDAP и создание промптов):

```json
[
   {"kind":"can_use", "target":"capability", "name":"auth.login"},
   {"kind":"can_use", "target":"capability", "name":"auth.login.ldap"},
   {"kind":"can_use", "target":"capability", "name":"prompt.create"}
]
```

Пример `owner.skill.payload`:

```json
[
   {"kind":"can_view",    "target":"created_object"},
   {"kind":"can_use",     "target":"created_object"},
   {"kind":"can_edit",    "target":"created_object"},
   {"kind":"can_publish", "target":"created_object"},
   {"kind":"can_delete",  "target":"created_object"},
   {"kind":"can_grant",   "target":"created_object"}
]
```

`capability` разрешается по `name`, `created_object` заменяется ID нового объекта.
Весь JSON проверяется до записи; неизвестная цель/действие/имя прерывает транзакцию.
Создание объекта и назначение обычных `app_grant` по выбранному сервером шаблону атомарны.
Набор применяется однократно: изменение defaults влияет только на последующие создания.
Отключённый набор не назначает прав; отсутствие обязательного набора считается ошибкой настройки.

#### UC10. Назначить первоначальные гранты из JSON

Внутренний шаг создания объекта/пользователя. Шаблон и получателя выбирает сервер;
клиент не может вызвать его для произвольного существующего объекта.

```sql
create or replace function aid.app_apply_grant_default(
   template_name varchar,
   recipient_id bigint,
   created_object_id bigint
)
returns integer
language plpgsql
as $$
declare
   template aid.app_grant_default%rowtype;
   invalid_count integer;
   changed_count integer;
begin
   -- Отсутствующий обязательный шаблон — ошибка, а не молчаливые пустые права.
   -- SHARE сохраняет один набор правил до завершения транзакции создания.
   select * into strict template
   from aid.app_grant_default
   where name = template_name
   for share;

   if not template.enable then
      return 0;
   end if;

   with rules as (
      select
         r.kind,
         r.target,
         case
            when r.target = 'created_object' then created_object_id
            when r.target = 'capability' then c.id
         end as target_id
      from
         jsonb_to_recordset(template.payload) as r(kind varchar, target varchar, name varchar)
         left join aid.app_capability as c
            on r.target = 'capability' and c.name = r.name
   )
   -- Проверяем ВСЕ правила до вставки. Неизвестные записи не отфильтровываются молча.
   select count(*) into invalid_count
   from rules as r
   left join aid.app_ident as i on i.id = r.target_id
   where
      r.kind is null
      or r.kind not in (
         'can_view', 'can_use', 'can_edit', 'can_publish', 'can_delete',
         'can_grant', 'can_manage_members', 'can_bind', 'can_revoke'
      )
      or r.target is null
      or r.target not in ('created_object', 'capability')
      or i.id is null;

   if invalid_count > 0 then
      -- Ошибка откатывает и предшествующее создание объекта в этой транзакции.
      raise exception 'invalid_grant_default: %', template_name using errcode = '22023';
   end if;

   with rules as materialized (
      select distinct
         r.kind,
         case when r.target = 'created_object' then created_object_id else c.id end as target_id
      from
         jsonb_to_recordset(template.payload) as r(kind varchar, target varchar, name varchar)
         left join aid.app_capability as c
            on r.target = 'capability' and c.name = r.name
   ), saved as (
      insert into aid.app_grant as g (id, obj_id, sbj_id, kind)
      select
         nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
         recipient_id,
         r.target_id,
         r.kind
      from
         rules as r
         left join aid.app_grant as old
            on old.obj_id = recipient_id and old.sbj_id = r.target_id and old.kind = r.kind
      where
         old.id is null or not old.enable
      order by r.target_id, r.kind
      on conflict (obj_id, sbj_id, kind) do update
         set enable = true, updated_at = now()
         where not g.enable
      returning id
   ), registered as (
      -- Ни provenance, ни ссылка на defaults в app_grant не записываются.
      insert into aid.app_ident (id, kind)
      select id, 'app_grant' from saved order by id
      on conflict (id) do nothing
   )
   select count(*) into changed_count from saved;

   return changed_count;
end;
$$;
```

Пример данных:

| Шаблон и `payload` | Аргументы | Новые строки `app_grant` |
| --- | --- | --- |
| `owner.connection`: `[{"kind":"can_use","target":"created_object"},{"kind":"can_grant","target":"created_object"}]` | Получатель `10`, новый объект `1100` | `(10,1100,can_use,true)`, `(10,1100,can_grant,true)` |
| `user.created`: `[{"kind":"can_use","target":"capability","name":"auth.login"}]`; capability имеет ID `202` | Новый пользователь `12` | `(12,202,can_use,true)` |
| Правило с `name="unknown.capability"` | Любые | Ошибка `22023`, транзакция откатывается |

Дубли правил не создают дубли грантов. Отключённый шаблон возвращает `0`.
Функция вызывается только при первоначальном создании: повторный вход или изменение
шаблона не должны заново выдавать ранее отозванные права.

Изменение шаблона администратором проходит обычную проверку `can_edit`:

```sql
-- $1 — исполнитель; $2 — ID шаблона; $3 — проверенный сервером JSON правил.
-- До записи проверяются kind, target и имена capability; created_object — будущий объект.
update aid.app_grant_default
set payload = $3, updated_at = now()
where
   id = $2
   and aid.app_has_permissions($1, array[$2], array['can_edit']::varchar[])
-- UPDATE ждёт завершения создания, удерживающего SHARE на этом шаблоне.
returning id;
```

Пример данных: шаблон `300 = owner.connection` назначал пять прав, новый `$3` содержит
только `can_view` и `can_use`. После обновления новые подключения получат два права;
гранты ранее созданного `1100` не меняются. Без `can_edit` шаблон не изменится.

#### UC11. Создать подключение с первоначальными правами

```sql
-- $1 — исполнитель/владелец; $2 — name; $3 — kind; $4 — payload.
-- $5 — новый ID из RETURNING, который сервис подставляет только во второй запрос.
begin;

with allocated as (
   insert into aid.app_ident (kind)
   select 'app_connection'
   where aid.app_has_permissions(
      $1,
      -- Неизвестное имя даёт NULL: проверка откажет, объект не появится.
      array[(select id from aid.app_capability where name = 'connection.create')],
      array['can_use']::varchar[]
   )
   returning id
)
insert into aid.app_connection (id, owner_id, name, kind, payload)
select id, $1, $2, $3, $4 from allocated
returning id;

-- Если RETURNING пуст — ROLLBACK. Иначе сохранить ID в $5.
-- Право создания включает первоначальную выдачу по фиксированному серверному шаблону.
-- can_grant на ещё не существовавший объект не требуется.
select aid.app_apply_grant_default('owner.connection', $1, $5);

commit;
```

Пример данных: capability `200 = connection.create`, у `10` есть `can_use` на `200`;
шаблон `owner.connection` содержит пять прав из таблицы defaults ниже.
Вход: `name='wiki'`, `kind='web'`, `payload={"host":"wiki.example"}`.
Если новый ID — `1100`, создаются connection с `owner_id=10` и пять грантов `(10,1100,kind)`.
Ошибка в defaults откатывает connection и все выделенные строки `app_ident`.

#### UC12. Создать промпт вместе с первой версией

```sql
-- $1 — исполнитель; $2 — name; $3 — description; $4 — content.
-- $5 — prompt_id из RETURNING, подставляемый сервисом во второй запрос.
begin;

with allocated as (
   insert into aid.app_ident (kind)
   select 'prompt'
   where aid.app_has_permissions(
      $1,
      array[(select id from aid.app_capability where name = 'prompt.create')],
      array['can_use']::varchar[]
   )
   returning id
), created as (
   -- revision обязательна и ссылается на первую версию.
   insert into aid.prompt (id, owner_id, kind, name, revision)
   select id, $1, 'prompt', $2, 1 from allocated
   returning id
)
-- Объект и его версия появляются одним оператором: оба встречных FK выполняются.
insert into aid.prompt_revision (prompt_id, revision, description, content, created_by)
select id, 1, $3, $4, $1 from created
returning prompt_id;

-- Пустой RETURNING: ROLLBACK; иначе его prompt_id сохранить в $5.
select aid.app_apply_grant_default('owner.prompt', $1, $5);
commit;
```

Пример данных: `prompt.create` разрешена `10`, вход `name='summary'`,
`description='Краткое резюме'`, `content='Составь резюме документа.'`.
Создаются `prompt(id=1100,owner_id=10,revision=1)`, `prompt_revision(1100,1)`
и обычные гранты владельца на `1100`. Для системного промпта обработчик выбирает
`system_prompt.create`, `kind='system_prompt'` и `owner.system_prompt`.

#### UC13. Первый вход с автоматическим созданием пользователя

Credentials и каноническое имя проверяются до транзакции. `sys_*` в выдаче грантов не участвуют.

```sql
-- $1 — доверенная identity auth-service; $2 — каноническое имя пользователя.
-- $3 — user_id, который сервер получил из одного из первых двух запросов.
-- $4 — имя capability провайдера, например auth.login.ldap; выбирается сервером.
begin;

with created as (
   insert into aid.app_identity (id, name, kind)
   select
      nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
      $2,
      'user'
   where aid.app_has_permissions(
      $1,
      array[(select id from aid.app_capability where name = 'identity.user.create')],
      array['can_use']::varchar[]
   )
   -- Предварительный SELECT существования не нужен: UNIQUE разрешает гонку входов.
   on conflict (kind, name) do nothing
   returning id
), registered as (
   insert into aid.app_ident (id, kind)
   select id, 'app_identity' from created
)
select id from created;

-- Если вернулся ID — created=true, сохранить его в $3.
-- Только если RETURNING пуст: выполнить следующий SELECT НОВЫМ оператором.
-- После ожидания ON CONFLICT он увидит пользователя, созданного другой транзакцией.
select id
from aid.app_identity
where kind = 'user' and name = $2;
-- Если пользователь по-прежнему отсутствует — ROLLBACK, создание не разрешено.

-- Выполнять ТОЛЬКО при created=true; существующим пользователям права не восстанавливать.
select aid.app_apply_grant_default('user.created', $3, $3);

-- Назначить read и подтверждённые role_sources роли запросами UC6 в этой транзакции.
-- Исполнитель — $1 (auth-service), участник — $3, право — can_manage_members на роль.
-- Для нескольких ролей сначала блокируется объединение всех нужных identity (UC6).

-- Проверяется уже сам пользователь, включая только что назначенные гранты/членство.
select aid.app_has_permissions(
   $3,
   array[
      (select id from aid.app_capability where name = 'auth.login'),
      (select id from aid.app_capability where name = $4)
   ],
   array['can_use', 'can_use']::varchar[]
) as allowed;

-- allowed=false: ROLLBACK вместе с новым пользователем, членством и грантами.
-- allowed=true: COMMIT, только затем выдаётся пользовательская сессия.
commit;
```

Пример данных: `50` может создавать пользователей и назначать роль `30`;
capabilities `202 = auth.login`, `203 = auth.login.ldap` включены и входят в `user.created`.

| Ситуация для имени `bob` | Результат |
| --- | --- |
| Пользователя ещё нет | Одна identity пользователя, один набор defaults, членство в `read` |
| Два одновременных первых входа | Пользователя создаёт одна транзакция; вторая читает его следующим SELECT и не применяет defaults |
| `auth.login.ldap` выключена | При первом входе всё созданное откатывается; сессия не выдаётся |
| Пользователь уже есть, его login-гранты отозваны | Defaults повторно не применяются; при отсутствии других разрешений вход запрещён |

### Первоначальная настройка каждого приложения

В каждой БД отдельно создаются следующие identity и обычные гранты. Все ID, включая
ID строк грантов и defaults, выделяются последовательностью местной `app_ident`
и регистрируются в ней в той же атомарной операции.
Начальная настройка не перезаписывает изменённые администратором записи при рестарте.

| Identity | Начальное назначение |
| --- | --- |
| `role/read` | Общая роль пользователей; автоматически назначается новому пользователю |
| `role/admin` | Администраторы; член `read`, но не имеет скрытых полномочий |
| `service/auth-service` | Внутренний обработчик входа; недоступен для пользовательской аутентификации |
| Первый `user` | Заранее выбранная доверенная учётная запись; член `admin`, не первый произвольный посетитель |

| Получатель | Объекты | Начальные `kind` |
| --- | --- | --- |
| `admin` | Каждая зарегистрированная capability | `can_view`, `can_use`, `can_edit`, `can_grant` |
| `admin` | Каждый набор defaults | `can_view`, `can_edit`, `can_grant` |
| `admin` | Роли `admin` и `read` | `can_view`, `can_edit`, `can_manage_members`, `can_grant` |
| `auth-service` | Capability `identity.user.create` | `can_use` |
| `auth-service` | Роли, назначаемые доверенными role_sources; первоначально `admin`, `read` | `can_manage_members` |

Первый администратор и служба создаются процедурой первоначальной настройки.
Остальные пользователи создаются после проверки credentials: `auth-service` проходит
обычную проверку `identity.user.create`, создаёт пользователя и применяет `user.created`.
Членство в `read` и ролях из проверенных role_sources назначается с проверкой
`can_manage_members` службы на каждую роль. Затем проверяется вход самого пользователя;
при отказе создание нового пользователя, его членство и гранты откатываются вместе.
Сессия выдаётся после успешного завершения; существующим пользователям defaults повторно не назначаются.

Общие capabilities для AID, BOBA и MCP:

| `name` | Первоначальное разрешение |
| --- | --- |
| `auth.login` | Все новые пользователи через `user.created` |
| `auth.login.<provider_name>` | По одной на каждую фактическую `sys_provider.name`; первоначально включены в `user.created` |
| `connection.create`, `auth.create` | Все новые пользователи; создают личные подключения/credentials |
| `identity.user.create` | `auth-service` и `admin` |
| `identity.role.create`, `identity.team.create` | `admin` |

`sys_*` не меняются и не содержат FK на прикладные таблицы. Обработчик входа выбирает
`auth.login.<provider_name>` по имени провайдера. Наличие capability не включает сам
провайдер и не заменяет проверку credentials. Новый провайдер требует регистрации
capability и явного назначения прав; действующие defaults/гранты автоматически не расширяются.

Общие наборы defaults в каждой БД:

| Имя набора | Права на `created_object` |
| --- | --- |
| `owner.connection` | `can_view`, `can_use`, `can_edit`, `can_delete`, `can_grant` |
| `owner.auth` | `can_view`, `can_use`, `can_edit`, `can_delete`, `can_grant`, `can_bind` |
| `owner.role`, `owner.team` | `can_view`, `can_edit`, `can_delete`, `can_grant`, `can_manage_members` |
| `owner.session` | `can_view`, `can_revoke` |

В таблицах наполнения каждое имя обозначает отдельную строку `app_grant_default`;
списки разворачиваются в JSON указанного формата. Ссылок между шаблонами нет.
Назначение `read` не отменяет личные права: права только складываются.
`admin` не получает автоматического доступа к чужим прикладным объектам.

## DDL таблиц системы хранения `app_connection`, `app_auth`, `app_session`

Эти таблицы обслуживают подключения, которые устанавливает приложение к MCP, LLM и другим внешним ресурсам.

Примеры ниже указываются без шифрования, однако в итоге шифрование секретных полей обязательно

```sql
-- параметры подключения к источнику
create table aid.app_connection (
   id          bigint      not null references aid.app_ident (id),
   owner_id    bigint      not null references aid.app_identity (id), -- создавший пользователь
   name        varchar     not null check (length(name) > 0),
   kind        varchar     not null check (length(kind) > 0),
   payload     jsonb       not null check (jsonb_typeof(payload) = 'object'),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id)
);
create index idx_app_connection_name on aid.app_connection(name);
create index idx_app_connection_kind_name on aid.app_connection(kind, name);
```

пример `payload`, `kind='web'`:

```json
{
   "host": "confl.loshara.com",
   "path": "",
   "port": 443,
   "ssl_verify": true,
   "retry_attempts": 3,
   "retry_statuses": {"401": 3, "429": 5}
}
```

```sql
-- авторизации с помощью которых можно авторизоваться в подключениях, описанных в boba.
-- здесь храняться конкретные способы авторизации:
-- kind - указывает тип авторизации
-- payload - все необходимое для совершения авторизации
create table aid.app_auth (
   id          bigint      not null references aid.app_ident (id),
   owner_id    bigint      not null references aid.app_identity (id), -- создавший пользователь
   kind        varchar     not null check (length(kind) > 0),
   payload     jsonb       not null check (jsonb_typeof(payload) = 'object'),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id)
);
create index idx_app_auth_kind on aid.app_auth(kind);
```

| `app_auth.kind` | Пример `payload`                        |
| --------------- | --------------------------------------- |
| `basic`         | `{"username":"alice","password":"..."}` |
| `bearer`        | `{"token":"..."}`                       |

другие примеры можно вывести из текущего кода, где есть авторизации в postgres, oracle, clickhouse, web


```sql
-- артефакты после аутентификации в исходящем подключении
create table aid.app_session (
   id             bigint      not null references aid.app_ident (id),
   connection_id  bigint      not null references aid.app_connection (id),
   auth_id        bigint      null references aid.app_auth (id),
   identity_id    bigint      null references aid.app_identity (id), -- владелец, если сессия персональная
   kind           varchar     not null check (length(kind) > 0),
   payload        jsonb       not null check (jsonb_typeof(payload) = 'object'),
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),

   primary key (id)
);
```

Пример расшифрованного `app_session.payload`, `kind='oauth2'`:

```json
{
   "access_token": "...",
   "refresh_token": "...",
   "token_type": "Bearer",
   "expires_at": "2026-11-07T15:00:00Z",
   "scope": ["inference"]
}
```

Сессии, которые быбли установлены с помощью `app_connection` и `app_auth` (подключились и прошли авторизацию) лежат в таблице aid.app_session.

таблица сессий используется для хранения артефактов, которые нужны для доступа после прохождения авторизации.

к примеру `oauth2` система авторизации выдает:
```json
{
   "access_token": "...",
   "refresh_token": "...",
   "token_type": "Bearer",
   "expires_at": "2026-11-07T15:00:00Z",
   "scope": ["inference"]
}
```

и для доступа далее требуется передавать `access_token` с каждым запросом.
А для продления доступа надо использовать `refresh_token`
поэтому все системы авторизации должны иметь следующую систему:
1. Взять из таблицы `app_auth` объект авторизации и пройти авторизацию
2. В случае успеха, положить в таблицу `app_session` артефакт, который требуется передавать с каждым запросом
3. когда сессионный объект протух и более невалиден, то он удаляется. Вся система должна быть заточена под то, что `app_auth` используется для первоначального старта и авторизации, а `app_session` используется для последующей работы в рамках сессии и при этом `app_auth` никак не трогается более
пока не протухнет сессия и не потребуется сделать новую. Грубо говоря `app_auth` нужен только для установки авторизации, такое разделение четко разделяет ответственность между компонентами


## DDL таблиц приложения AID

### Начальные права AID

Общие identity, capabilities и defaults создаются по разделу первоначальной настройки.
Дополнительно регистрируются следующие capabilities; `admin` получает на них общие
начальные гранты, а столбец «Новый пользователь» определяет состав `user.created`.

| Capability | Назначение | Новый пользователь |
| --- | --- | --- |
| `prompt.create` | Личный обычный промпт | `can_use` |
| `system_prompt.create` | Личный системный промпт | Не назначается |
| `skill.create` | Личный скил | `can_use` |
| `memory.create` | Личная запись памяти; изменение чужого контекста требует права на него | `can_use` |
| `mcp.create` | Запись MCP-сервера в личном реестре | `can_use` |
| `thread.create` | Чат пользователя | `can_use` |

| `app_grant_default.name` | Начальный `payload` (развернуть в JSON-массив правил) |
| --- | --- |
| `user.created` | `can_use` на `auth.login`, все первоначальные `auth.login.<provider_name>`, `connection.create`, `auth.create`, `prompt.create`, `skill.create`, `memory.create`, `mcp.create`, `thread.create` |
| `owner.prompt`, `owner.system_prompt`, `owner.skill` | На `created_object`: `can_view`, `can_use`, `can_edit`, `can_publish`, `can_delete`, `can_grant` |
| `owner.memory`, `owner.mcp`, `owner.thread` | На `created_object`: `can_view`, `can_use`, `can_edit`, `can_delete`, `can_grant` |

`system_prompt.create` можно отдельно выдать автору или роли через обычный `can_grant`.
Для общего MCP-сервера его создатель выдаёт `read` права `can_view` / `can_use`;
если используются отдельные connection/auth, также выдаётся `can_use` на них.
Defaults позволяют создавать личные объекты, но адреса серверов и credentials нужно заполнить.

```sql
-- общие таблицы sys_* используются также и в приложении aid
-- общие таблицы app_* используются также и в приложении aid


-- реестр добавленных mcp серверов.
-- те mcp, которые пользователи видят в своем окружении.
-- это просто список доступных mcp с которыми взаимодействует пользователь.
create table aid.mcp (
   id          bigint      not null references aid.app_ident (id),
   owner_id    bigint      not null references aid.app_identity (id), -- создавший пользователь
   name        varchar     not null check (length(name) > 0),
   description varchar     not null check (length(description) > 0),
   -- здесь храниться остальная информация о подключении к mcp
   -- как в app_connection, храниться объект, с помощью которого мы можем сделать сессию
   -- здесь храниться тип авторизации, авторизационные данные если они нужны и прочее
   payload     jsonb       not null check (jsonb_typeof(data) = 'object'),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id)
);
```

Реестры промптов, скилов и памяти, используемых LLM.
Гранты выдаются на постоянные `id` из `prompt`, `skill`, `memory`.
Версии промптов и скилов — состояния этих объектов, без отдельных `app_ident`.

```sql
-- обычные и системные промпты; любой из них может быть шаблоном
create table aid.prompt (
   id          bigint      not null references aid.app_ident (id),
   owner_id    bigint      not null references aid.app_identity (id),
   -- prompt, system_prompt
   kind        varchar     not null check (length(kind) > 0),
   name        varchar     not null check (length(name) > 0),
   -- текущая версия, указатель на версию в prompt_revision
   revision    integer     not null check (revision > 0),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id),
   unique (owner_id, kind, name)
);

-- неизменяемая версия текста и параметров шаблонизации
create table aid.prompt_revision (
   prompt_id       bigint      not null references aid.prompt (id),
   revision        integer     not null check (revision > 0),
   description     varchar     not null check (length(description) > 0),
   content         varchar     not null check (length(content) > 0),
   content_type    varchar     not null default 'text/markdown' check (length(content_type) > 0),
   -- null: готовый текст; иначе механизм шаблонизации, например jinja2
   template_engine varchar     null check (length(template_engine) > 0),
   -- JSON Schema входных параметров шаблона
   input_schema    jsonb       null check (jsonb_typeof(input_schema) = 'object'),
   created_by      bigint      not null references aid.app_identity (id),
   created_at      timestamptz not null default now(),

   primary key (prompt_id, revision),
   check ((template_engine is null) = (input_schema is null))
);

-- выбранная версия обязательно принадлежит этому промпту
alter table aid.prompt add constraint fk_prompt_revision
   foreign key (id, revision)
   references aid.prompt_revision (prompt_id, revision);
```

Для готового текста `template_engine` и `input_schema` равны `null`.
Для `content = 'Подготовь описание проекта {{ project_name }}.'`,
`template_engine = 'jinja2'` пример `input_schema`:

```json
{
   "type": "object",
   "properties": {
      "project_name": {"type": "string", "description": "Название проекта"}
   },
   "required": ["project_name"],
   "additionalProperties": false
}
```

```sql
-- скилы в общем реестре; одинаковые имена допустимы у разных владельцев
create table aid.skill (
   id          bigint      not null references aid.app_ident (id),
   owner_id    bigint      not null references aid.app_identity (id),
   -- постоянное имя из SKILL.md, совпадает с именем папки скила
   name        varchar     not null check (length(name) between 1 and 64),
   -- выбранная версия; null, пока версия не выбрана
   revision    integer     null check (revision > 0),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id),
   unique (owner_id, name)
);

-- неизменяемый снимок всей папки скила
create table aid.skill_revision (
   skill_id       bigint      not null references aid.skill (id),
   revision       integer     not null check (revision > 0),
   -- извлекается из SKILL.md при публикации
   description    varchar     not null check (length(description) between 1 and 1024),
   -- содержимое папки, включая подкаталоги; SKILL.md находится в корне архива
   archive        bytea       not null check (octet_length(archive) > 0),
   archive_format varchar     not null default 'tar.gz'
                              check (archive_format = 'tar.gz'),
   archive_sha256 varchar(64) not null
                              check (archive_sha256 ~ '^[0-9a-f]{64}$'),
   -- остальные атрибуты frontmatter SKILL.md, извлечённые из архива
   payload        jsonb       not null default '{}'::jsonb
                              check (jsonb_typeof(payload) = 'object'),
   created_by     bigint      not null references aid.app_identity (id),
   created_at     timestamptz not null default now(),

   primary key (skill_id, revision)
);

alter table aid.skill add constraint fk_skill_revision
   foreign key (id, revision)
   references aid.skill_revision (skill_id, revision);
```

Пример `skill_revision.payload`:

```json
{
   "license": "MIT",
   "compatibility": "Requires Python 3",
   "metadata": {"author": "example-team"}
}
```

Публикация скила: сервис забирает папку из workspace, проверяет `SKILL.md`
и соответствие имени, формирует архив и извлекает описание и атрибуты.
Загрузка версии распаковывает её в `/workspace/skills/{id}/{revision}/{name}/`
и возвращает агенту путь к `SKILL.md`. Пути и ссылки не должны выходить за пределы папки.

Строки `prompt_revision` и `skill_revision` только добавляются;
запрет `UPDATE`/`DELETE` закрепляется правами БД или триггером.
Добавление версии и переключение `prompt.revision` / `skill.revision`
выполняются в одной транзакции с блокировкой строки объекта.

```sql
-- отдельная изменяемая запись памяти: текст или структурированные данные
create table aid.memory (
   id         bigint      not null references aid.app_ident (id),
   owner_id   bigint      not null references aid.app_identity (id),
   -- область применения: пользователь, команда, thread, проект
   scope_id   bigint      not null references aid.app_ident (id),
   -- источник, если он зарегистрирован как объект приложения
   source_id  bigint      null references aid.app_ident (id),
   -- preference, fact, decision, summary и другие
   kind       varchar     not null check (length(kind) > 0),
   name       varchar     not null check (length(name) > 0),
   -- обычный текст или Markdown
   content    varchar     null check (length(content) > 0),
   payload    jsonb       null check (jsonb_typeof(payload) in ('object', 'array')),
   -- счётчик изменений текущей строки, без хранения истории
   revision   integer     not null default 1 check (revision > 0),
   expires_at timestamptz null,
   enable     boolean     not null default true,
   created_at timestamptz not null default now(),
   updated_at timestamptz not null default now(),

   primary key (id),
   check ((content is not null) <> (payload is not null))
);

create index idx_memory_scope_kind
   on aid.memory (scope_id, kind) where enable;
create index idx_memory_owner on aid.memory (owner_id);
```

Текстовая память: `kind = 'decision'`, `content = 'Настройки авторизации
перечитываются по SIGHUP.'`, `payload = null`.
Структурированная память: `kind = 'preference'`, `content = null`, пример `payload`:

```json
{
   "language": "ru",
   "verbosity": "short",
   "examples": true
}
```

При изменении памяти проверяется ожидаемая `revision`, счётчик увеличивается,
`updated_at` обновляется. В обычную выдачу входят записи с `enable = true`
и `expires_at is null or expires_at > now()`. `owner_id` и `scope_id`
не заменяют проверку доступа через `app_grant`.

Чаты пользователей в chainlit
к конкретному чату многое привязывается, 

```sql
create table aid.thread (
   id          bigint      not null references aid.app_ident (id),
   owner_id    bigint      not null references aid.app_identity (id), -- создавший пользователь
   name        varchar     not null check (length(name) > 0),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id)
);
create index idx_threads_updated_name on aid.threads(updated_at desc, name);

create table aid.thread_
```

## Целевая схема ddl агента BOBA

### Начальные права BOBA

Общие identity, capabilities и defaults создаются в БД BOBA независимо от AID.

| Capability | Назначение | Новый пользователь |
| --- | --- | --- |
| `thread.create` | Диалог агента | `can_use` |
| `llm_provider.create` | Добавление провайдера инференса | Не назначается |
| `llm_model.create` | Добавление модели; также требуется `can_edit` на провайдер | Не назначается |

| `app_grant_default.name` | Начальный `payload` (развернуть в JSON-массив правил) |
| --- | --- |
| `user.created` | `can_use` на `auth.login`, все первоначальные `auth.login.<provider_name>`, `connection.create`, `auth.create`, `thread.create` |
| `owner.thread`, `owner.llm_provider`, `owner.llm_model` | На `created_object`: `can_view`, `can_use`, `can_edit`, `can_delete`, `can_grant` |

Для первого рабочего диалога администратор добавляет хотя бы один провайдер и модель,
а также необходимые connection/auth. На общие провайдер и модель выдаёт роли `read`
`can_view` / `can_use`, на используемые connection/auth — `can_use`.
При инференсе проверяются модель, провайдер и фактически используемые connection/auth
одним списком требований; наличие настроек само по себе разрешения не даёт.
Чтение журнала проверяет `can_view` на thread, продолжение диалога — `can_use` на thread;
на строки journal отдельные гранты не выдаются.

```sql
-- общие таблицы app_ident, app_identity, app_identity_member, app_capability,
-- app_grant_default, app_grant, app_connection, app_auth, app_session
-- и таблицы провайдеров входа создаются по описанию выше


-- таблица всех сессий агента, которые когда либо были зарегистрированы
-- владелец хранится явно; доступ других identity задаётся грантами
create table boba.thread (
   id          bigint      not null references boba.app_ident (id),
   owner_id    bigint      not null references boba.app_identity (id), -- создавший пользователь
   -- сюда можно записать любое имя для сессии
   -- acp протокол генерирует любое название и заполняет его
   name        varchar     not null check (length(name) > 0),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),
   
   primary key(id)
);
create index idx_threads_updated_name on boba.thread(updated_at desc, name);


-- таблица истории, которую ведет агент в своем формате хранения истории
-- Эта история преобразуется в формат acp и отдается по протоколу acp
-- в будущем мы можем реализовать поддержку других протоколов, напирмер openai
-- которое скорее всего должно уйти, потому что в payload уже содержиться kind, означающее примерно тоже самое
-- и это нужно будет проверить
create table boba.journal (
   id          bigint      not null references boba.app_ident (id),
   thread_id   bigint      not null references thread (id),
   -- последовательность сообщений для thread_id
   seq         integer     not null,
   -- тип данных внутри payload, ранее это поле было разбито на два: type и kind внутри payload
   -- но это надо проанализировать, скорее всего одного поля kind вполне достаточно
   -- два поля скорее всего появились вследствие недостаточного рефакторинга
   kind        varchar     not null check(length(kind) > 0)
   -- поле хранит итоговую нагрузку. ранее это поле называлось record
   payload     jsonb       not null check (jsonb_typeof(data) = 'object'),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),
   
   primary key (thread_id, seq desc)
);
create index idx_journal_thread_updated_at on boba.journal(thread_id, updated_at desc);


-- реестр llm провайдеров с которыми работает boba
-- llm провайдеры это объект который знает тип провайдера и знает как к нему подключиться
-- llm провайдер хранит данные о том, какой протокол общения используется в провайдере
-- сейчас в коде уже поддерживаются 3 варианта:
-- openai - openai совместимый провайдер
-- onnx - onnx локальный инференс (в основном для маленьких моделей)
-- ollama - ollama совместимый провайдер
-- fastembeding - не уверен что это отдельный тип провайдера
-- payload должен содержать сериализованые данные без указания конкретного app_connection
-- содержиться все необходимое, что потребуется для работы с конкретным провайдером
-- я пока не продумал этот объект. Но смысл в том, что агностичные данных по транспорту храняться в app_connection
-- а здесь информация о конкретном llm провайдере
create table boba.llm_provider (
   id             bigint      not null references boba.app_ident (id),
   owner_id       bigint      not null references boba.app_identity (id), -- создавший пользователь
   name           varchar     not null check(length(name) > 0),
   payload        jsonb       not null check (jsonb_typeof(data) = 'object'),
   -- объект который используется как транспорт подключения к llm провайдеру (например http транспорт)
   -- не факт что connection_id вообще нужен для подключения, к примеру когда используется локальный onnx
   -- поэтому поле nullable
   connection_id  bigint      null references boba.app_connection (id),
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),

   primary key (id)
);


-- реестр моделей, которые доступы в конкретных llm провайдерах
-- именно этот реестр определяет набор моделей, которые acp протокол возвращает в процессе создании сессии
-- обновление этого реестра будет производиться строго определенной командой в boba (первоначально через SIGHUP (механизм уже существует, в него надо добавить это))
-- но только если провайдер поддерживает возможность обновления. К примеру openai провайдер возвращает модели через /v1/models
-- но может быть ситуация когда /v1/models просто недоступен, либо сам провайдер неподдерживает обновление
-- соответственно администратор системы может добавлять сюда записи вручную
-- также здесь храниться набор параметров, которые возвращаются через acp протокол в configOptions
-- такие как temperature, top_k, context_size и прочие.
create table boba.llm_provider_model (
   id                bigint      not null references boba.app_ident (id),
   owner_id          bigint      not null references boba.app_identity (id), -- создавший пользователь
   llm_provider_id   bigint      not null references llm_provider (id),
   name              varchar     not null check(length(name) > 0),
   -- здесь храняться все параметры, которые можно отправлять в инференс
   payload           jsonb       not null check (jsonb_typeof(data) = 'object'),
   created_at        timestamptz not null default now(),
   updated_at        timestamptz not null default now(),

   primary key (id)
);
-- вот предлагаемый вариант хранения payload для различных провайдеров. 
-- смысл поля в том что бы дать свободу в том, какие параметры мы здесь храним и какие параметры можем переопределять
-- схема самостоятельно описывает какие именно параметры доступы, какие у них типы и варианты значений
-- а пользователь фактически будет выбирать из указанных параметров
-- defaults будут применяться там где нет явно переданного значения от пользователя
-- вот мой пример:
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "temperature": {
      "title": "Temperature",
      "description": "Температура sampling",
      "type": "number",
      "minimum": 0,
      "maximum": 2,
      "enum": [0.2, 0.7, 1.0],
      "default": 0.7
    },
    "top_k": {
      "title": "Top K",
      "description": "Количество кандидатов при sampling",
      "type": "integer",
      "minimum": 1,
      "enum": [20, 40, 80],
      "default": 40
    }
  }
}

```

## Начальные права MCP

Общие таблицы `app_*`, identity и defaults создаются в БД MCP.
Для инструментов используются capabilities по имени плагина из конфигурации:
одна capability разрешает инструменты всего плагина. Отдельная SQL-схема endpoint/tool
в этом документе пока не определена; новые таблицы инструментов здесь не вводятся.

| Capability | Назначение | Новый пользователь |
| --- | --- | --- |
| `tool.bash.execute` | Shell-инструменты в workspace пользователя | `can_use` |
| `tool.doc.execute` | Обработка документов | `can_use` |
| `tool.web.execute` | Web-инструменты | `can_use` |
| `tool.kb.execute` | Поиск в настроенной базе знаний | `can_use` |
| `tool.pg.execute` | Инструменты работы с БД | Не назначается |
| `tool.ingest.execute` | Загрузка данных в базу знаний | Не назначается |

| `app_grant_default.name` | Начальный `payload` (развернуть в JSON-массив правил) |
| --- | --- |
| `user.created` | `can_use` на `auth.login`, все первоначальные `auth.login.<provider_name>`, `connection.create`, `auth.create`, `tool.bash.execute`, `tool.doc.execute`, `tool.web.execute`, `tool.kb.execute` |

Наборы `owner.connection`, `owner.auth`, `owner.role`, `owner.team`, `owner.session`
совпадают по начальному содержимому с общими наборами, но хранятся в БД MCP.
При первоначальном заполнении включаются только capabilities установленных плагинов;
соответственно формируется полный JSON `user.created`. Права на новый плагин после
первоначальной настройки выдаются явно, defaults при рестарте не переписываются.

Обработчик вызова определяет плагин по зарегистрированному инструменту и проверяет
`can_use` на `tool.<plugin>.execute` общей проверкой. Дополнительно включаются требования
на явно выбранные connection/auth. Права внешней БД и ограничения sandbox остаются
условиями выполнения; capability не расширяет их. Workspace выбирается по пользователю.

AID, BOBA и MCP независимо сопоставляют подтверждённого пользователя своим identity.
Между сервисами передаётся проверяемый контекст пользователя; локальные числовые ID
и identity `auth-service` не используются для подмены пользователя в другом сервисе.
Доступ к записи MCP-сервера в AID не заменяет разрешение выполнить инструмент в MCP.
