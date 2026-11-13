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

   # Системная авторизация: sys_auth_provider, sys_role_provider, sys_oauth_client.
   # Связи провайдеров с источниками — sys_auth_role.

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

Каждый плагин имеет самостоятельный конфиг: свои пути, connections и инференс.
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

   # Системная авторизация: sys_auth_provider, sys_role_provider, sys_oauth_client.
   # Связи провайдеров с источниками — sys_auth_role.

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

   # Системная авторизация: sys_auth_provider, sys_role_provider, sys_oauth_client.
   # Связи провайдеров с источниками — sys_auth_role.

[logging]
   loggers = { "uvicorn.access" = { level = "WARNING" } }
```

## Назначение настроек

| Поле / секция                   | Назначение                                                                                                                                 |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `server`                        | Адрес прослушивания, порт, URL-префикс, публичный URL и TLS                                                                                |
| AID `server.mcp_apps.port`      | Отдельный порт MCP Apps, отличающийся от порта чата; host и TLS общие с AID                                                                |
| `paths`                         | Расширяемый словарь путей; произвольные имена доступны через `${paths.<имя>}`                                                              |
| `paths.runtime`                 | Заранее подготовленные ресурсы приложения, только чтение                                                                                   |
| `paths.data`                    | Создаваемые приложением данные, включая постоянные файлы и рабочие кэши                                                                    |
| `paths.sandbox` / `certs`       | Имена для интерполяции путей к образам и сертификатам в примерах                                                                           |
| `paths.workspace` / `tool_logs` | Постоянные пользовательские образы / журналы инструментов                                                                                  |
| AID `paths.upload_cache`        | Временные файлы загрузок Chainlit                                                                                                          |
| `paths.kerberos_cache`          | Рабочие ccache; keytab и `krb5.conf` размещаются в `krb/` рядом с основным конфигом                                                        |
| BOBA `paths.models`             | Подготовленные ONNX-модели: подкаталог по имени модели. В AID пути к весам не хранятся                                                     |
| `database.postgres`             | connection, авторизация и пул; база `aid`, для отладки `aid_ss`. `schema` необязательна в модели connection; хранилище задаёт нужную схему |
| Плагин `tables.schema`          | Схема набора таблиц; по умолчанию `database.postgres.schema`. В kb поиск использует `ix`, таблицы чанков — `kb`                            |
| `workspace`                     | `template` — шаблон; `user_image` — постоянный образ пользователя; `sandbox_path` — точка монтирования для инструмента                     |
| `tools.launcher`                | Способ запуска и общая ветка `cgroup_base` экземпляра MCP                                                                                  |
| Плагин `sandbox`                | Программа, rootfs, mounts, монтирование workspace и лимиты конкретного плагина                                                             |
| Плагин `sandbox.mounts`         | `путь в MCP:путь в песочнице`, только чтение. Пути OCR/эмбеддингов указывают на назначение mount                                           |
| Плагин `ocr` / `embedding`      | HTTP-провайдер либо локальный движок с подготовленными моделями. У kb/ingest модель и размерность эмбеддингов совпадают                    |
| MCP tools                       | Инструменты предоставляются манифестами плагинов; выполнение проверяет `can_use` на capability `tool.<plugin>.execute`                     |
| AID `messaging`                 | `database` — connection PostgreSQL; `node_id` — уникальное имя узла                                                                        |
| `logging`                       | Переопределения стандартного dictConfig                                                                                                    |

Пути относятся к файловой системе процесса сервиса; `sandbox_path` и назначения
mounts — к песочнице инструмента. Само объявление имени в `paths` каталог не создаёт.

### Авторизация

Провайдеры входа, источники ролей и их credentials хранятся в БД.
В TOML остаются connection к самой БД, секретные ключи для шифрования данных внутри базы, шифрования сессий и прочего, другая мелочь

### Секретные ключи

| Настройка                     | Назначение                                                                                             |
| ----------------------------- | ------------------------------------------------------------------------------------------------------ |
| `server.storage_key`          | Шифрование секретов `app_auth`, содержимого `app_session` и столбцов `credential` в системных таблицах |
| `server.session_key`          | Подпись JWT и шифрование OAuth-состояния; общий у реплик службы                                        |
| `auth.session.credential_key` | Шифрование передаваемых Kerberos credentials; общий у обменивающихся ими служб                         |

Ключи шифрования остаются вне БД. Ключи и shared secrets: 32 случайных байта,
URL-safe Base64. Шифрование — Fernet, подписи — HS256/HMAC-SHA256,
внутренние ключи назначения выводятся через HKDF-SHA256.

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```


## DDL таблиц управления приложением

В приложении используются таблицы:
- sys_auth_provider - провайдеры атворизации в приложении
- sys_role_provider - источник ролей, которые назначаются после авторизации
- sys_auth_role - таблица сшивает провайдеры авторизации и источники ролей
- sys_oauth_client - доверенные приложения oauth которые проходят через proxy авторизацию

При старте приложение читает активные настройки для работы сервиса авторизации и собирает их в памяти. Далее приложение не читает таблицы, все необходимое храниться в памяти до момента когда придет сигнал SIGHUP который запускает обновление конфига и прочего. В этот момент системные таблицы перечитываются и начинают действовать новые настройки.


```sql
-- здесь я использую схему aid для примера
-- те же таблицы будут в приложениях boba и mcp
-- так как система основа у них общая
create schema aid;

-- провайдеры авторизации
create table aid.sys_auth_provider (
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

Обрати внимание что примеры ниже отображают те модели, которые уже есть в коде с той разницей, что они перенесены в базу данных, в отдельные поля. Однако же, сейчас система авторизации в целом готова, и требуется в основном сменить источник ее конфигураций.

Так или иначе, в примерах описана целевая форма конфигурирования к которой я стремлюсь и нужно постараться найти несоответствия с ней, указать мне и вместе решить что с этим делать.

Так мог бы выглядеть `payload` и `credential` для приложения:

```json
{
   "name": "local_users",
   "kind": "local",
   "payload": {},
   "credential": {
      "users": {
         "local_user": "$argon2id$..."
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
-- источники ролей со своими правилами и параметрами connection
create table aid.sys_role_provider (
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
         {"by": "login", "value": "first", "roles": ["admin"]},
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
Правила назначают роли по имени из `sys_identity(kind='role')`;
совпадение `deny=true` запрещает вход.
Proxy-источник получает роли из проверенного утверждения.

```sql
-- таблица объединяет провайдеры авторизации и источники ролей, которые присвоены этим провайдерам авторизации
create table aid.sys_auth_role (
   role_auth_name       varchar     not null references aid.sys_auth_provider (name),
   role_provider_name   varchar     not null references aid.sys_role_provider (name),
   enable               boolean     not null default true,
   created_at           timestamptz not null default now(),
   updated_at           timestamptz not null default now(),

   primary key (role_auth_name, role_provider_name)
);
```

Примеры строк связи из исходного конфига:

| `name`          | `role_provider_name` |
| --------------- | -------------------- |
| `local_users`   | `local`              |
| `ldap`          | `ldap`               |
| `kerberos`      | `kerberos`           |
| `kerberos`      | `ldap`               |
| `proxy`         | `proxy`              |
| `proxy`         | `ldap`               |
| `proxy`         | `local`              |

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

Подтверждённый логин сопоставляется с `sys_identity(kind='user')`.
Пользователи, членство и прикладные grants остаются в `app_`.

## DDL таблиц приложения (общие таблицы для приложений aid, boba, mcp)

Префикс `app_` используется для общих таблиц приложения.
Ниже используется схема `aid`, но в приложениях boba, mcp будет использоваться собственная.

В основном здесь будет описываться система ролевого доступа к объектам приложения.
Система ролевого доступа держиться на 4-х таблицах.

Любой объект в системе имеет свой адрес, состоящий из: <таблица> и <id> записи.
В ролейвой модели участвуют следующие сущности:
- permissions - типы связей между объектами: use, read, edit, delete, grant
- grants - таблица связей с permission объектов:
**src** и **src_id** - кому выдано право
**tgt** и **tgt_id** - на что выдано право
**permission** - какое выдано право
- default_grants - таблица прав, которые назначаются по умолчанию, в процессе работы системы, к примеру при создании нового пользователя, connection, skill и так далее
- capability - таблица возможностей, которые явно невозможно связать. К примеру можно ли авторизоваться?, можно ли удалять?
grants связывает запись пользователей (роли, команды) с capability через id записей, точно также, как связывает этих же пользователей с connection, skill, prompt и другими таблицами, объекты которых контролируются ролевой моделью.


```sql
-- таблица пользователей, ролей, команд
-- на уровне таблицы нет различия между user, role, team, это все одни записи одного порядка, у которых сквозной id
-- таблица выполнена в виде набора идентификаторов c name
-- членство identity в другом identity задаётся через sys_identity_member, там по сути храниться иерархия identity
-- это можно использовать что-бы в приложении создавать identity, которые будут выступать как роли или команды
-- но по факту, это обычные identity. Различие будет наблюдаться лишь в наборе grants
create table aid.sys_identity (
   id          bigserial   not null,
   -- имя уникально среди всех identity
   name        varchar     not null check (length(name) > 0),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),
   primary key (id)
);
create unique index uk_sys_identity_name on aid.sys_identity(name);

-- таблица содержит вложение identity друг в друга
-- роли, команды, пользователи
create table aid.sys_identity_member (
   id             bigserial   not null,
   -- identity_id команды, в которую входит member_id (тут имеется ввиду все что угодно, это может быть роль, команда и любая другая группирующая identity_id которую создали что бы быть командой)
   team_id        bigint      not null references aid.sys_identity (id),
   -- identity_id участника команды
   member_id      bigint      not null references aid.sys_identity (id),
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),
   check (member_id <> team_id),
   
   primary key (id)
);
create unique index uk_sys_identity_member on aid.sys_identity_member(team_id, member_id);


-- таблица sys_grant содержит взаимосвязи, которые означают "какие права выданы объекту"
create table aid.sys_grant (
   id             bigserial   not null,
   -- получаетль прав
   -- identity содержит название таблицы в которой есть identity_id указывающий на полчателя прав
   -- обычно получатель прав это одна из записей в таблице identity
   identity_id    bigint      not null,
   -- право (разрешение) которое выдано
   access         varchar     not null check (access in ('can_read', 'can_use', 'can_edit', 'can_delete', 'can_create', 'can_grant')),
   -- объект, на который выдается разрешение
   taget          varchar     not null,
   -- id объекта, на который выдается разрешение
   target_id      bigint      not null,
   -- вкл/выкл
   enable         boolean     not null default true,
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),

   primary key (id)
);
create index idx_sys_grant_identity_target_target_id on aid.sys_grant(identity_id, access, target, target_id) using (enable);


-- Реестр возможностей приложения, например connection, skill, auth.login.
-- Конкретное действие задаётся в sys_grant.access: can_create, can_use и т. д.
-- Grant на capability позволяет проверить право, когда конкретного объекта ещё нет
-- или действие не связано с отдельным прикладным объектом, например вход в приложение.
create table aid.sys_capability (
   id          bigserial   not null,
   -- имя capability. Эти имена знает и приложение, поэтому может забрать id capability для проверки в таблице grants
   name        varchar     not null check (length(name) > 0),
   description varchar     not null check (length(description) > 0),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id)
);
create unique index uk_sys_capability on aid.sys_capability(name);


-- Когда создается новый объект очень часто нужно раздать права на объекты
-- К примеру раздать права на новый созданный объект другим, а также раздать права тем кто должен получить права на новый объект (к примеру самому создателю объекта)
-- В это таблице указывается набор прав, который нужно раздать и кому нужно раздать
-- Приложение не угадывает это, оно просто берет от сюда этот набор
-- Наборы прав имеют имена, эти же имена должно использовать приложение, что бы выбирать корректные наборы
-- Администратор системы может менять наборы default прав если захочет. Задача программиста продумать начальные значения и заполнить таблицу тем, что он считает значениями по умолчаиню
create table aid.sys_default_grant (
   id             bigserial   not null,
   -- имена default grants, которые ищет приложение, что бы выдать их для identity.
   -- эти имена знает приложение (они будут перечислены в коде)
   -- так приложение и база связывают возможности
   name           varchar     not null check (length(name) > 0),
   -- получатель прав в виде псевдонима для объекта, которому выдаются права
   -- псевдоним нужен потому что конкретный объект известен только в момент совершения действия из capability
   -- - creaator - объект, который воспользовался capability и создал объект
   -- - new - объект, который был создан тем, кто воспользовался capability
   recipient   varchar     not null check (recipient in ('creaator', 'new')),
   -- здесь указываюся права, которые должны быть розданы в виде json сериализованного объекта
   grant       jsonb       not null check (jsonb_typeof(credential) = 'object'),
   description varchar     not null check (length(description) > 0),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id)
);
create unique index uk_sys_default_grant on aid.sys_default_grant(name, recipient);
-- Пример:
-- - Alice (identity_id = 10) создал postgres connection с именем pg (id = 1).
-- В этом случае предполагается, что создателю объекта выдаются все права на этот объект.
-- Для того, что бы описать такие права можно использовать следующую запись в таблице default_grant:
insert into sys_default_grant(name, recipient, grant, description) values 
(
   "connection_create",
   "creaator",
   [ "can_use", "can_edit", "can_read", "can_delete", "can_grant" ],
   "Права для владельца конекшина"
);
-- здесь connection_create это именованое действие, которое храниться как в коде приложения, так и в базе данных.
-- Приложение ничего не знает о том, какие права надо выдавать в случае connection_create.
-- его задача спросить ролевую модель об этом и просто выдать эти права.
-- поэтому приложение запрашивает действие "connection_create" для "creaator" (для того, кто создал объект) в ролевой модели и получает в ответ: [ "can_use", "can_edit", "can_read", "can_delete", "can_grant" ]
-- если права не вернулись или пустой массив прав, то права не выдаются
```

### Действия и единая проверка grants

**Возможные grants**

Grant разрешает `identity_id` выполнить действие `access` над объектом, указанным в `target` и `target_id`.

| `access`     | Что разрешает                                                                                                               |
| ------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `can_create` | Создать новый объект соответствующего вида. Выдаётся на capability, поскольку ID нового объекта ещё нет.                     |
| `can_read`   | Прочитать данные объекта; пароли и токены остаются скрыты.                                                                    |
| `can_use`    | Использовать конкретный ресурс, например connection или credentials; на capability — воспользоваться указанной возможностью. |
| `can_edit`   | Изменить объект; добавить участника в role/team, добавить или выбрать версию prompt/skill.                                    |
| `can_delete` | Удалить объект, например prompt/skill, connection или сессию. Убрать участника из role/team — по этому праву на группу.       |
| `can_grant`  | Выдать grant на объект, в том числе на capability. Нужно также иметь само выдаваемое право на этот объект.                    |

**Возможные capabilities**

Capability — именованная возможность приложения. Сама запись в `sys_capability`
не даёт разрешения: grant связывает `identity_id` с этой записью и задаёт `access`.
Так проверяются действия, для которых ещё нет конкретного объекта или он не нужен:
создание нового connection, создание роли, вход в приложение, вызов инструментов.

Имя обозначает возможность, а `access` — действие:
- `connection` + `can_create`
- `skill` + `can_create`
- `auth.login` + `can_use`

В таблице перечислены возможности целевой модели и права на их выполнение.
Это каталог допустимых действий, а не права, автоматически выдаваемые пользователям.

| `sys_capability.name`       | `access`     | Приложение     | Что разрешает grant на capability                                                                                         |
| --------------------------- | ------------ | -------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `auth.login`                | `can_use`    | AID, BOBA, MCP | Войти в приложение после проверки credentials провайдером входа.                                                          |
| `connection`                | `can_create` | AID, BOBA, MCP | Создать новый connection в `app_connection`.                                                                              |
| `auth`                      | `can_create` | AID, BOBA, MCP | Добавить новые credentials в `app_auth`.                                                                                  |
| `session`                   | `can_create` | AID, BOBA, MCP | Создать исходящую `app_session`; дополнительно нужен `can_use` на connection и используемые credentials.                  |
| `identity.user`             | `can_create` | AID, BOBA, MCP | Создать новую identity пользователя после проверки его credentials провайдером входа.                                     |
| `identity.role`             | `can_create` | AID, BOBA, MCP | Создать новую identity, используемую как роль.                                                                            |
| `identity.team`             | `can_create` | AID, BOBA, MCP | Создать новую identity, используемую как команда.                                                                         |
| `prompt`                    | `can_create` | AID            | Создать новый обычный prompt, в том числе шаблон, вместе с первой версией.                                                |
| `system_prompt`             | `can_create` | AID            | Создать новый system prompt, в том числе шаблон, вместе с первой версией.                                                 |
| `skill`                     | `can_create` | AID            | Добавить новый skill в реестр вместе с первой версией.                                                                    |
| `memory`                    | `can_create` | AID            | Создать новую запись memory; запись в memory дополнительно требует `can_edit` на него.                                    |
| `mcp`                       | `can_create` | AID            | Добавить новый MCP-сервер в реестр.                                                                                       |
| `thread`                    | `can_create` | AID, BOBA      | Создать новый thread для диалога.                                                                                         |
| `llm_provider`              | `can_create` | BOBA           | Добавить нового провайдера инференса.                                                                                     |
| `llm_model`                 | `can_create` | BOBA           | Добавить новую модель в `llm_provider_model`; дополнительно нужен `can_edit` на её провайдер.                             |

Право создать объект не даёт прав на созданный объект. После создания объекта, если не будут назначены права на него (вообще никакие)
то этот объект останется без прав доступа вообще и даже создатель этого объекта не сможет к нему обратиться. В ролейвой системе нет понятия owner'а объекта.
Набор прав по умолчанию хранится в `sys_default_grants` для конкретного именованного действия. Именованое действие используется в приложении и по нему производиться поиск прав которые нужно раздать на объект, после создания объекта.
Например, `connection.create.payload` перечисляет права создателя на новый connection.
Пустой массив `[]` означает, что первоначальные grants не выдаются.

#### Найти ID по имени и имя по ID

```sql
-- Имя identity → ID.
create or replace function aid.sys_identity_id(identity_name varchar)
returns bigint
language sql stable
as $$
   select
      i.id
   from
      aid.sys_identity as i
   where
      i.name = identity_name;
$$;

-- ID identity → имя.
create or replace function aid.sys_identity_name(identity_id bigint)
returns varchar
language sql stable
as $$
   select
      i.name
   from
      aid.sys_identity as i
   where
      i.id = identity_id;
$$;

-- Имя capability → ID.
create or replace function aid.sys_capability_id(capability_name varchar)
returns bigint
language sql stable
as $$
   select
      c.id
   from
      aid.sys_capability as c
   where
      c.name = capability_name;
$$;

-- ID capability → имя.
create or replace function aid.sys_capability_name(capability_id bigint)
returns varchar
language sql stable
as $$
   select
      c.name
   from
      aid.sys_capability as c
   where
      c.id = capability_id;
$$;
```

#### `app_principals`: найти identity и все группы, от которых она получает права

`identity_id` — ID строки `sys_identity`: пользователя, службы, роли или команды.
Функция возвращает этот ID и ID всех identity, в которые она входит напрямую
или через другие группы.
Членство берётся из `sys_identity_member`.
Например, для пользователя вернутся он сам, его команда и роль этой команды.

```sql
create or replace function aid.app_principals(identity_id bigint)
returns table(id bigint)
language sql stable
as $$
   with recursive principals(id) as (
      select
         i.id
      from
         aid.sys_identity as i
      where
         i.id = identity_id
      union
      select
         m.member_id
      from
         principals as p
         join aid.sys_identity_member as m on m.identity_id = p.id
   )
   select
      id
   from
      principals;
$$;
```

Пример членства по именам:

| Имя участника | Имя родительской identity |
| ------------- | ------------------------- |
| `alice`       | `dev`                     |
| `dev`         | `read`                    |

```sql
select
   aid.sys_identity_name(p.id) as name
from
   aid.app_principals(aid.sys_identity_id('alice')) as p;
```

Результат — имена `alice`, `dev`, `read`. Неизвестное имя даст пустой результат.

#### `app_has_permissions`: проверить все запрошенные права identity

Учитывает grants самой identity и всех групп из `app_principals`. Возвращает `true`, только если разрешено всё запрошенное, иначе `false`.

| Аргумент      | Что передать                                                     |
| ------------- | ---------------------------------------------------------------- |
| `identity_id` | Чьи права проверяем, например `aid.sys_identity_id('alice')`     |
| `targets`     | ID выбранных объектов, например connections `wiki` и `search`    |
| `actions`     | Какие права нужны на эти объекты, например `{can_view,can_edit}` |

Первое право из `actions` относится к первому объекту, второе — ко второму.
Например, для connections `wiki` и `search` список `{can_view,can_edit}` означает:
«можно ли посмотреть `wiki` и изменить `search`?» Для двух прав на один объект его ID указывают дважды.

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
      -- Первое право — для первого объекта, второе — для второго
      select distinct
         r.id,
         r.action
      from
         -- Одна строка на каждую пару: targets[1] и actions[1], targets[2] и actions[2], ...
         unnest(targets, actions) as r(id, action)
   ), available as (
      -- Права пользователя и его групп читаем вместе.
      select distinct
         g.target_id as id,
         g.action
      from
         -- app_principals - возвращает всю иерархию наверх от identity_id
         aid.app_principals(identity_id) as p
         join aid.sys_grant as g on g.recipient_id = p.id
         left join aid.sys_capability as c on c.id = g.target_id
      where 1=1
         and g.enable
         and g.target_id = any(targets)
         -- У connection или промпта строки в sys_capability нет — это допустимо.
         -- can_use запрещён только при enable=false; NULL означает, что это не capability.
         and (g.action <> 'can_use' or c.enable is not false)
   )
   select
      -- Для каждого объекта должно найтись именно запрошенное право.
      -- Пустой список, NULL и списки разной длины не дают разрешения.
      count(*) > 0 and count(*) = count(a.id)
   from
      required as r
      left join available as a on
             a.id   = r.id
         and a.action = r.action;
$$;
```

```sql
-- для поиска по имени можно использовать функции резолвинга по имени
select
   aid.app_has_permissions(
      aid.sys_identity_id('boba'),
      array[
         aid.sys_capability_id('auth.login'),
         aid.sys_capability_id('auth.login.ldap')
      ],
      array['can_use','can_use']::varchar[]
) as allowed;
```

Пример данных:
- членство `alice → dev → read`
- у `read` есть `can_use` на connection `wiki`.
- У `alice` есть `can_edit` на `wiki` и `can_use` на отключённую capability `auth.login.ldap`.

| Проверка для `aid.sys_identity_id('alice')` | Результат                        |
| ------------------------------------------- | -------------------------------- |
| `can_use` на connection `wiki`              | `true`: право получено от `read` |
| `can_use` и `can_edit` на connection `wiki` | `true`: разрешены оба действия   |
| `can_delete` на connection `wiki`           | `false`: права нет               |
| `can_use` на capability `auth.login.ldap`   | `false`: capability отключена    |
| Пустые `targets` и `actions`                | `false`                          |

#### `app_can_grant_to`: можно ли передать право выбранной identity

Получателем может быть сама выдающая identity или identity, в которую она входит прямо или через другие identity. Правило одинаково для всех `kind`.

`app_can_grant_to` функция проверяет получателя, можно ли ему передать право.
`aid.app_has_permissions(...)` проверяет, есть ли у выдающего `can_grant` и выдаваемое право на объект. Обе проверки объединены в запросе выдачи grant.

| Аргумент       | Что передать                             |
| -------------- | ---------------------------------------- |
| `identity_id`  | Кто выдаёт право: его `sys_identity.id`  |
| `recipient_id` | Кому выдаём право: его `sys_identity.id` |

Результат: `true` — получатель допустим, `false` — нет.

```sql
create or replace function aid.app_can_grant_to(identity_id bigint, recipient_id bigint)
returns boolean
language sql stable
as $$
   select
      count(*) > 0
   from
      -- Сама identity и все identity, в которые она входит; kind не проверяется.
      aid.app_principals(identity_id) as p
   where 1=1
      and p.id = recipient_id;
$$;
```

Пример данных: `alice → dev → read`.

```sql
select aid.app_can_grant_to(
   aid.sys_identity_id('alice'),
   aid.sys_identity_id('dev')
) as allowed;
```

Результат — `true`. Получатели `alice` и `read` также допустимы.
Получатели `bob`, `other`, `auth-service` без такой связи дают `false`, независимо от `kind`.
Неизвестное имя выдающего или получателя также даёт `false`.

#### Проверить права перед использованием ресурсов

```sql
-- $1 — id выбранного MCP с именем wiki-tools.
-- $2 — id выбранного connection с именем wiki.
-- $3 — auth_id выбранной app_session для wiki.
select aid.app_has_permissions(
   aid.sys_identity_id('alice'),
   array[$1, $2, $3]::bigint[],
   array['can_use', 'can_use', 'can_use']::varchar[]
) as allowed;
```

Пример данных: у `alice` есть `can_use` на MCP `wiki-tools`, connection `wiki`
и credentials выбранной сессии `wiki`. Результат — `allowed = true`.
Если хотя бы одного права нет, результат — `false`. У `app_auth` нет поля `name`,
поэтому credentials здесь определяются через `auth_id` сессии.
При выборе credentials проверяется `can_use` и на них, и на connection.
Изменение сохранённых настроек connection требует `can_edit` на него.
Для изменения объекта проверка стоит прямо в `UPDATE`; отдельно полученное `true` для записи не используется.

#### Показать или изменить connection

```sql
-- $1 — ID пользователя, который хочет посмотреть connection.
-- $2 — ID connection (app_connection.id).
-- Без can_view строка не возвращается.
select
   c.id,
   c.name,
   c.kind,
   c.payload
from
   aid.app_connection as c
where 1=1
   and c.id = $2
   and aid.app_has_permissions($1, array[$2], array['can_view']::varchar[]);
```

Пример данных: connection `wiki` имеет `payload = {"host":"old.example"}`.
В `$1` передаётся `aid.sys_identity_id('alice')`, в `$2` — ID выбранного `wiki`.
При наличии `can_view` возвращается эта строка; без права — пустой результат.

```sql
-- $1 — ID пользователя, который меняет connection.
-- $2 — ID connection;
-- $3 — новые настройки в JSON, например {"host":"new.example"}.
-- Проверка встроена в UPDATE: без can_edit данные не изменятся.
-- Одновременные UPDATE одной строки выполняются по очереди.
update
   aid.app_connection
set
   payload = $3,
   updated_at = now()
where 1=1
   and id = $2
   and aid.app_has_permissions($1, array[$2], array['can_edit']::varchar[])
returning
   id;
```

Пример данных: у `alice` есть `can_edit` на connection `wiki` с настройками `{"host":"old.example"}`.
Параметры: `$1 = aid.sys_identity_id('alice')`, `$2` — ID выбранного `wiki`, `$3 = {"host":"new.example"}`.
Настройки обновляются; возвращается ID этого `wiki`. Без права строк в результате нет, настройки остаются прежними.

#### Выдать одно право пользователю или группе

Создаёт grant, если его нет, или включает отключённый. Уже включённый grant не меняется.
`allowed` показывает, разрешена ли выдача; `changed` — изменились ли данные.

```sql
-- $1 — ID того, кто выдаёт право;
-- $2 — ID того, кому выдаём (sys_identity.id).
-- $3 — ID объекта (app_ident.id);
-- $4 — название права, например 'can_use'.
with permitted as materialized (
   -- Чтобы выдать can_use, нужны оба права на этот объект: can_grant и can_use.
   select
      aid.app_has_permissions(
         $1, array[$3, $3], array['can_grant', $4]::varchar[]
      ) and aid.app_can_grant_to($1, $2) as allowed
), saved as (
   insert into aid.sys_grant as g (id, recipient_id, target_id, action)
   select
      nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
      $2,
      $3,
      $4
   from
      permitted as p
      left join aid.sys_grant as current_grant
         on current_grant.recipient_id = $2
         and current_grant.target_id = $3
         and current_grant.action = $4
   where
      p.allowed
      -- Если право уже включено, пропускаем запись и не блокируем эту строку.
      and (current_grant.id is null or not current_grant.enable)
   -- При двух одновременных вставках одинакового grant сохраняется одна строка.
   on conflict (recipient_id, target_id, action)
   do update
   set enable = true,
       updated_at = now()
   where
         not g.enable
   returning id
), registered as (
   -- В app_ident записываем ID сохранённого grant: лишних строк после конфликта нет.
   -- Обе таблицы записываются одним SQL; ссылки проверяются в конце этого SQL.
   insert into aid.app_ident (id, kind)
   select id, 'sys_grant' from saved
   on conflict (id) do nothing
)
select
   allowed,
   (select count(*) from saved) > 0 as changed
from
   permitted;
```

Пример данных: `alice → dev`; у `alice` есть `can_grant` и `can_use` на connection `wiki`.
У `dev` этого grant ещё нет. Параметры: `$1 = aid.sys_identity_id('alice')`,
`$2 = aid.sys_identity_id('dev')`, `$3` — ID выбранного `wiki`, `$4 = 'can_use'`.

| Ситуация                                                             | Результат                      | Данные после запроса                                               |
| -------------------------------------------------------------------- | ------------------------------ | ------------------------------------------------------------------ |
| Первая выдача                                                        | `allowed=true, changed=true`   | `dev` получает `can_use` на `wiki`; создан grant и его `app_ident` |
| Повторная выдача                                                     | `allowed=true, changed=false`  | Строка и её ID не меняются                                         |
| Выдача после отключения grant                                        | `allowed=true, changed=true`   | Та же строка включена, прежний ID сохранён                         |
| У `alice` нет `can_grant`                                            | `allowed=false, changed=false` | Ничего не записано                                                 |
| Получатель `bob`, связи `alice → bob` нет даже через другие identity | `allowed=false, changed=false` | Ничего не записано                                                 |

#### Отключить одно выданное право

```sql
-- $1 — ID того, кто отзывает право; $2 — ID того, у кого отзываем (sys_identity.id).
-- $3 — ID объекта (app_ident.id); $4 — название отзываемого права.
-- Для отзыва can_use нужны can_revoke и can_use на этом же объекте.
update
   aid.sys_grant
set
   enable = false,
   updated_at = now()
where
   recipient_id = $2
   and target_id = $3
   and action = $4
   and enable
   and aid.app_has_permissions(
      $1, array[$3, $3], array['can_revoke', $4]::varchar[]
   )
-- Для отзыва членство в identity получателя не требуется.
returning
   id;
```

Пример данных: у `alice` есть `can_revoke` и `can_use` на connection `wiki`.
Параметры: `$1 = aid.sys_identity_id('alice')`, `$2 = aid.sys_identity_id('bob')`,
`$3` — ID выбранного `wiki`, `$4 = 'can_use'`.
Grant, разрешавший `bob` использовать `wiki`, получает `enable=false`; возвращается ID этого grant.
Без `can_revoke` или самого отзываемого права строка не меняется; `can_grant` их не заменяет.
Повторный отзыв ничего не меняет. Если Bob получает то же право от роли, оно остаётся.
Отзыв ещё не созданного grant ничего не меняет: одновременный запрос выдачи может
создать этот grant после отзыва.

#### Выдать несколько прав одной операцией

Если хотя бы одно право выдавать нельзя, ни одно из прав списка не записывается.

```sql
-- $1 — ID пользователя, который выдаёт права.
-- $2 — JSON-массив: recipient_id — кому выдаём, target_id — на какой объект, action — какое право.
begin;

-- Сначала блокируем ID получателей и объектов, чтобы их не удалили до конца операции.
-- Другим выдачам KEY SHARE не мешает.
-- Если какой-то ID не найден — ROLLBACK, ничего не записываем.
with requested as (
   select * from jsonb_to_recordset($2) as r(recipient_id bigint, target_id bigint, action varchar)
), referenced as (
   select recipient_id as id from requested
   union
   select target_id from requested
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
   -- Убираем повторы: одно право одному получателю на один объект пишется один раз.
   select distinct recipient_id, target_id, action
   from jsonb_to_recordset($2) as r(recipient_id bigint, target_id bigint, action varchar)
), needed as (
   select target_id, action from requested
   union
   select target_id, 'can_grant' from requested
), permitted as materialized (
   -- Сначала проверяем, разрешено ли выдать каждое право каждому получателю.
   select
      (select aid.app_has_permissions($1, array_agg(target_id), array_agg(action)) from needed)
      and coalesce(
         (select bool_and(aid.app_can_grant_to($1, recipient_id)) from requested), false
      ) as allowed
), saved as (
   insert into aid.sys_grant as g (id, recipient_id, target_id, action)
   select
      nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
      r.recipient_id,
      r.target_id,
      r.action
   from
      requested as r
      cross join permitted as p
      left join aid.sys_grant as old
         on old.recipient_id = r.recipient_id
         and old.target_id = r.target_id
         and old.action = r.action
   where
      p.allowed
      and (old.id is null or not old.enable)
   -- Записываем всегда в одном порядке: получатель, объект, право.
   -- Иначе два запроса могут захватить разные строки и ждать друг друга.
   order by r.recipient_id, r.target_id, r.action
   on conflict (recipient_id, target_id, action) do update
      set enable = true, updated_at = now()
      where not g.enable
   returning id
), registered as (
   insert into aid.app_ident (id, kind)
   select id, 'sys_grant' from saved order by id
   on conflict (id) do nothing
)
select
   coalesce(allowed, false) as allowed,
   (select count(*) from saved) as changed
from permitted;

-- allowed=false: ROLLBACK; allowed=true: COMMIT.
commit;
```

Пример данных: у `alice` есть `can_grant` и `can_use` на connections `wiki` и `search`;
членство — `alice → dev → read`. У получателей ещё нет указанных grants.
Следующий SELECT собирает JSON для параметра `$2` запроса выдачи выше:

```sql
-- Только для этого SELECT: $1 — ID выбранного wiki, $2 — ID выбранного search.
select jsonb_build_array(
   jsonb_build_object('recipient_id', aid.sys_identity_id('dev'),
                     'target_id', $1::bigint, 'action', 'can_use'),
   jsonb_build_object('recipient_id', aid.sys_identity_id('read'),
                     'target_id', $2::bigint, 'action', 'can_use'),
   jsonb_build_object('recipient_id', aid.sys_identity_id('dev'),
                     'target_id', $1::bigint, 'action', 'can_use')
) as grants;
```

Выдачу выполняет `aid.sys_identity_id('alice')`. Результат — `allowed=true, changed=2`:
`dev` получает `can_use` на `wiki`, `read` — на `search`; повтор первой записи не создаёт третью строку.
Если не хватает права или `app_can_grant_to` вернула `false` хотя бы для одного получателя —
`allowed=false, changed=0`: весь набор остаётся без изменений.

#### Добавить участника в команду или роль

Участником может быть пользователь, служба или другая группа. Нельзя создавать
замкнутое членство: например, команда A входит в B, а B — обратно в A.
Допустимость сочетания identity проверяет один обработчик изменения членства.
Остальные запросы используют сохранённые связи без условий по `kind`.

```sql
-- $1 — ID того, кто добавляет участника.
-- $2 — кого добавляем; $3 — куда добавляем (оба — sys_identity.id).
-- $4 — список ID из первого SELECT (bigint[]); его заполняет само приложение.
begin;

-- 1. Найти группу $3, группы выше неё и участника $2. Сохранить их ID в $4.
select id from aid.app_principals($3)
union
select $2;

-- 2. Все запросы изменения членства сначала блокируют эти строки sys_identity.
-- Берём их в порядке ID. Если какая-то строка уже удалена — ROLLBACK.
select id
from aid.sys_identity
where id = any($4)
order by id
for no key update;

-- 3. Пока ждали, состав групп мог измениться. Читаем его заново.
-- acyclic=true: группа найдена, а добавление не замкнёт членство само на себя.
-- closure_locked=true: все найденные сейчас ID уже заблокированы.
-- acyclic=false: отказ. closure_locked=false: ROLLBACK и повторить с новым списком ID.
select
   count(*) > 0 and count(*) filter (where id = $2) = 0 as acyclic,
   count(*) filter (where not (id = any($4))) = 0 as closure_locked
from aid.app_principals($3);

-- 4. Теперь проверяем can_edit на группу $3 и записываем членство.
with permitted as materialized (
   select aid.app_has_permissions(
      $1, array[$3], array['can_edit']::varchar[]
   ) as allowed
), allocated as (
   insert into aid.app_ident (kind)
   select 'sys_identity_member'
   where
      (select allowed from permitted)
      -- Участник $2 заблокирован: другой такой запрос подождёт. Повторную связь не создаём.
      and (select count(*) from aid.sys_identity_member
           where identity_id = $2 and member_id = $3) = 0
   returning id
), inserted as (
   insert into aid.sys_identity_member (id, identity_id, member_id)
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

Пример данных: `dev → read`. У `auth-service` есть `can_edit` на `dev`.
Для добавления Bob: `$1 = aid.sys_identity_id('auth-service')`, `$2 = aid.sys_identity_id('bob')`,
`$3 = aid.sys_identity_id('dev')`. В таблице заблокированные строки указаны по именам.

| Добавление                                    | Заблокированные identity           | Результат                                                                    |
| --------------------------------------------- | ---------------------------------- | ---------------------------------------------------------------------------- |
| `bob → dev`                                   | `bob`, `dev`, `read`               | `acyclic=true, closure_locked=true`; связь создаётся                         |
| Повторное `bob → dev`                         | `bob`, `dev`, `read`               | `allowed=true, changed=false`                                                |
| `read → dev`                                  | `dev`, `read`                      | `acyclic=false`: получился бы цикл `read → dev → read`                       |
| После первого чтения появилась `read → other` | Старый набор: `bob`, `dev`, `read` | `closure_locked=false`: повторить, добавив `other` в набор блокируемых строк |

Этот порядок обязателен для всех запросов изменения членства. Если добавляем несколько
связей, сначала собираем и блокируем общий список ID, затем записываем все связи.

#### Убрать участника из команды или роли

```sql
-- $1 — ID того, кто убирает участника.
-- $2 — кого убираем; $3 — из какой группы (оба — sys_identity.id).
-- Нужен can_delete на группу $3; сама группа и участник остаются.
begin;

-- Берём ту же блокировку участника, что и при добавлении
select id from aid.sys_identity where id = $2 for no key update;

with removed as (
   delete from aid.sys_identity_member
   where
      identity_id = $2
      and member_id = $3
      and aid.app_has_permissions(
         $1, array[$3], array['can_delete']::varchar[]
      )
   returning id
)
-- Вместе со связью удаляем её строку app_ident.
-- Если на этот ID ссылаются другие записи, БД отменит удаление.
delete from aid.app_ident as i
using removed as r
where i.id = r.id
returning i.id;

commit;
```

Пример данных: связь `bob → dev`; у `auth-service` есть `can_delete` на `dev`.
Параметры: `$1 = aid.sys_identity_id('auth-service')`, `$2 = aid.sys_identity_id('bob')`,
`$3 = aid.sys_identity_id('dev')`.
Удаляются эта строка `sys_identity_member` и её `app_ident`. После сохранения удаления
следующая проверка прав `bob` не учитывает связь с `dev`. Повтор ничего не меняет.

#### Сохранить новую версию промпта и выбрать её для использования

Текст новой версии добавляется в `prompt_revision`. Чтобы приложение использовало
эту версию, нужно также записать её номер в `prompt.revision`.

```sql
-- $1 — ID автора новой версии; $2 — ID существующего промпта (prompt.id).
-- $3 — описание версии; $4 — её текст.
begin;

-- Два запроса добавления версии одного промпта выполнятся по очереди.
select id from aid.prompt where id = $2 for no key update;
-- Если строка не найдена — ROLLBACK.

insert into aid.prompt_revision (
   prompt_id, revision, description, content, created_by
)
select
   $2,
   -- Берём номер после последней сохранённой версии, а не после выбранной текущей.
   coalesce(max(revision), 0) + 1,
   $3,
   $4,
   $1
from aid.prompt_revision
where prompt_id = $2
having aid.app_has_permissions($1, array[$2], array['can_edit']::varchar[])
returning revision;

-- Если версия не добавлена — ROLLBACK. Если нужна только новая версия — COMMIT.
-- Чтобы сразу выбрать её текущей, перед COMMIT выполнить следующий UPDATE.
commit;
```

Пример данных: у промпта `summary` есть версии `{1,2,3}`, но `prompt.revision=1`.
В `$1` передаётся `aid.sys_identity_id('alice')`, в `$2` — ID выбранного `summary`.
Новая версия получит номер `4`, но приложение пока будет использовать `1`. Второй
одновременный запрос дождётся первого и создаст версию `5`, а не вторую `4`. Для `skill_revision` действует тот же порядок.

```sql
-- $1 — ID пользователя, который выбирает версию; $2 — ID промпта.
-- $3 — номер версии, которую приложение должно использовать.
-- Можно выполнить отдельно или до COMMIT добавления версии выше.
update aid.prompt
set revision = $3, updated_at = now()
where
   id = $2
   and aid.app_has_permissions($1, array[$2], array['can_edit']::varchar[])
-- Ссылка (id, revision) разрешит выбрать только существующую версию этого промпта.
-- Если одновременно добавляли версию, но UPDATE не вернул строку — ROLLBACK всей операции.
returning id, revision;
```

Пример данных: у промпта `summary` выбрана версия `1`, версия `4` существует;
у `alice` есть `can_edit` на этот промпт. Параметры: `$1 = aid.sys_identity_id('alice')`,
`$2` — ID выбранного `summary`, `$3 = 4`. После запроса возвращаются ID `summary` и номер версии `4`.
Без `can_edit` ничего не меняется. Если версии нет, БД вернёт ошибку ссылки;
в общей транзакции отменится и добавление новой версии.

#### Удалить connection и выданные на него права

```sql
-- $1 — ID пользователя, который удаляет connection; $2 — ID connection.
begin;

-- Новые grants, ссылающиеся на этот ID, дождутся завершения удаления.
-- Для удаления нескольких объектов сначала блокируем все их ID по возрастанию.
select id from aid.app_ident where id = $2 for update;

with removed_object as (
   delete from aid.app_connection
   where
      id = $2
      and aid.app_has_permissions($1, array[$2], array['can_delete']::varchar[])
   returning id
), removed_grants as (
   delete from aid.sys_grant as g
   using removed_object as o
   where g.target_id = o.id
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

-- Если connection ещё используется в app_session, БД не даст его удалить.
-- Тогда не удалятся ни connection, ни его права.
commit;
```

Пример данных: у connection `wiki` есть два grants — `can_delete` для `alice` и `can_use` для `dev`.
Другие записи на этот connection не ссылаются. Параметры: `$1 = aid.sys_identity_id('alice')`,
`$2` — ID выбранного `wiki`. Удаляются connection, оба grants и три строки `app_ident`;
возвращается ID удалённого `wiki`.
Если на `wiki` ссылается `app_session.connection_id`, БД отменяет всё удаление: connection ещё используется сессией.

### Начальная выдача прав

Одна capability определяет разрешение на создание и начальные права на результат.
Проверяется `can_use` на её ID; после создания `payload` этой же capability применяется
к новому объекту. Получатель — создавшая объект identity. Всё выполняется в одной транзакции.

Пример `payload` у capability `connection.create`:

```json
["can_view", "can_use", "can_edit", "can_delete", "can_grant", "can_revoke"]
```

`payload = []` оставляет объект без grants, в том числе для создателя.
Отсутствующая или выключенная capability запрещает саму операцию создания.
Ошибка в `payload` отменяет создание. Изменение `payload` не меняет ранее выданные grants.
Для `auth.login`, `auth.login.<name>` и `tool.*.execute` используется `[]`:
эти операции не назначают первоначальные права на новый объект реестра.

#### Выдать создателю начальные права на новый объект

`app_apply_initial_grants` вызывается обработчиком сразу после создания объекта,
в той же транзакции. Она вставляет обычные grants из `sys_capability.payload`,
без сохранения их связи с capability. На существующий объект повторно набор не применяется.

| Аргумент            | Что передать                                                                                     |
| ------------------- | ------------------------------------------------------------------------------------------------ |
| `capability_id`     | ID capability, по которой разрешено создание; запрос создания возвращает его вместе с ID объекта |
| `recipient_id`      | ID создавшей объект identity, например `aid.sys_identity_id('alice')`                            |
| `created_object_id` | ID только что созданного объекта из `RETURNING`                                                  |

Результат — число добавленных или включённых grants; при `payload = []` — `0`.
Это внутренний шаг разрешённого создания, а не способ выдавать права на существующие объекты.

```sql
create or replace function aid.app_apply_initial_grants(
   capability_id bigint,
   recipient_id bigint,
   created_object_id bigint
)
returns integer
language plpgsql
as $$
declare
   capability aid.sys_capability%rowtype;
   changed_count integer;
begin
   -- Читаем capability по тому же ID, который использовался при создании.
   -- До конца транзакции изменение её payload или enable подождёт.
   select * into capability
   from aid.sys_capability
   where id = capability_id
   for share;

   -- Если capability удалили или выключили между созданием и этим шагом,
   -- отменяем всю транзакцию, включая созданный объект.
   if not found then
      raise exception 'capability_not_found: %', capability_id using errcode = '22023';
   end if;
   if not capability.enable then
      raise exception 'capability_disabled: %', capability_id using errcode = '42501';
   end if;

   with actions as materialized (
      -- Повторяющиеся названия дают один grant; пустой массив — ни одного.
      select distinct r.action
      from jsonb_array_elements_text(capability.payload) as r(action)
   ), saved as (
      insert into aid.sys_grant as g (id, recipient_id, target_id, action)
      select
         nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
         app_apply_initial_grants.recipient_id,
         created_object_id,
         a.action
      from
         actions as a
         left join aid.sys_grant as old
            on old.recipient_id = app_apply_initial_grants.recipient_id
            and old.target_id = created_object_id
            and old.action = a.action
      where
         old.id is null or not old.enable
      order by a.action
      -- CHECK/NOT NULL в sys_grant отвергнут неверное действие и откатят создание.
      -- При повторной вставке сохраняем прежний ID grant.
      on conflict on constraint uk_sys_grant_recipient_target_action do update
         set enable = true, updated_at = now()
         where not g.enable
      returning id
   ), registered as (
      -- Регистрируем только ID сохранённых grants, без лишних строк после конфликта.
      insert into aid.app_ident (id, kind)
      select id, 'sys_grant' from saved order by id
      on conflict (id) do nothing
   )
   select count(*) into changed_count from saved;

   return changed_count;
end;
$$;
```

Пример: `alice` создаёт connection `wiki`; функция получает ID `connection.create`, ID `alice` и ID нового `wiki`.

| `connection.create.payload` | Результат                                                                                     |
| --------------------------- | --------------------------------------------------------------------------------------------- |
| `["can_use", "can_grant"]`  | `alice` может использовать `wiki` и выдавать `can_use` своей группе; отзывать grants не может |
| `[]`                        | `wiki` сохраняется без grants; у создателя нет доступа                                        |
| `["unknown_action"]`        | Ошибка CHECK; создание `wiki` и grants откатывается                                           |

Изменение начальных прав требует `can_edit` на соответствующую capability:

```sql
-- $1 — ID того, кто меняет набор; $2 — sys_capability.id, например sys_capability_id('connection.create').
-- $3 — JSON-массив названий action, например ["can_view", "can_use"].
-- Перед UPDATE обработчик проверяет элементы; CHECK таблицы проверяет только тип массива.
update aid.sys_capability
set payload = $3, updated_at = now()
where
   id = $2
   and aid.app_has_permissions($1, array[$2], array['can_edit']::varchar[])
-- Если payload сейчас применяется, UPDATE дождётся окончания той транзакции.
returning id;
```

Пример: в `connection.create.payload` было шесть действий, новый `$3` — `["can_view", "can_use"]`.
При следующем создании автор получит два grants. Права на уже созданный `wiki` не изменятся.
Для создания без начальных прав задают `payload = []`; `enable = false` запрещает создание.

#### Создать connection и выдать его автору начальные права

```sql
-- $1 — ID identity, которая создаёт connection и получит начальные grants.
-- $2 — имя connection; $3 — тип, например 'web'; $4 — настройки в JSON.
-- $5 — ID нового connection; $6 — ID capability. Оба берутся из RETURNING первого запроса.
begin;

with allocated as (
   insert into aid.app_ident (kind)
   select 'app_connection'
   where aid.app_has_permissions(
      $1,
      -- Неизвестное имя даёт NULL: проверка откажет, объект не появится.
      array[aid.sys_capability_id('connection.create')],
      array['can_use']::varchar[]
   )
   returning id
)
insert into aid.app_connection (id, created_by, name, kind, payload)
select id, $1, $2, $3, $4 from allocated
returning id, aid.sys_capability_id('connection.create') as capability_id;

-- Пустой RETURNING: ROLLBACK. Иначе id сохранить в $5, capability_id — в $6.
-- Выдать автору права из payload этой capability; это часть разрешённого создания.
-- Отдельный can_grant для этой первоначальной выдачи не требуется.
select aid.app_apply_initial_grants($6, $1, $5);

commit;
```

Пример данных: у `alice` есть `can_use` на capability `connection.create`;
`payload` этой capability содержит шесть прав из таблицы первоначального наполнения ниже.
Вход: `$1 = aid.sys_identity_id('alice')`, `name='wiki'`, `kind='web'`, `payload={"host":"wiki.example"}`.
Создаётся connection `wiki`, его `created_by` равен `aid.sys_identity_id('alice')`.
Автор получает шесть grants на новый connection. Оба ID из `RETURNING` передаются в `app_apply_initial_grants`.
При `payload = []` connection сохраняется без grants; без действующей capability создание запрещено.
Ошибка в наборе откатывает создание connection и всех выделенных строк `app_ident`.

#### Создать промпт вместе с первой версией

```sql
-- $1 — ID автора; $2 — имя промпта; $3 — описание; $4 — текст промпта.
-- $5 — ID нового промпта; $6 — ID capability. Оба берутся из RETURNING первого запроса.
begin;

with allocated as (
   insert into aid.app_ident (kind)
   select 'prompt'
   where aid.app_has_permissions(
      $1,
      array[aid.sys_capability_id('prompt.create')],
      array['can_use']::varchar[]
   )
   returning id
), created as (
   -- Сразу выбираем первую версию: revision=1.
   insert into aid.prompt (id, created_by, kind, name, revision)
   select id, $1, 'prompt', $2, 1 from allocated
   returning id
)
-- prompt ссылается на версию 1, а prompt_revision — на prompt.
-- Вставляем обе строки одним SQL, чтобы к проверке ссылок они уже существовали.
insert into aid.prompt_revision (prompt_id, revision, description, content, created_by)
select id, 1, $3, $4, $1 from created
returning prompt_id, aid.sys_capability_id('prompt.create') as capability_id;

-- Пустой RETURNING: ROLLBACK; иначе prompt_id сохранить в $5, capability_id — в $6.
select aid.app_apply_initial_grants($6, $1, $5);
commit;
```

Пример данных: у `alice` есть `can_use` на capability `prompt.create`.
Вход: `$1 = aid.sys_identity_id('alice')`, `name='summary'`,
`description='Краткое резюме'`, `content='Составь резюме документа.'`.
Создаются промпт `summary` с `created_by = aid.sys_identity_id('alice')`, `revision=1`
и первая `prompt_revision`. Grants автору выдаются из `prompt.create.payload`; при `[]` grants нет.
Для системного промпта обработчик выбирает capability `system_prompt.create` и `kind='system_prompt'`.

#### Создать пользователя при первом входе и проверить право войти

До этих запросов приложение проверяет пароль или токен у провайдера входа.
При первом создании пользователь входит в `read` и получает права этой роли.
При повторном входе это членство автоматически не восстанавливается, начальные grants не выдаются.

```sql
-- $1 — sys_identity.id службы входа, которая создаёт пользователя.
-- $2 — подтверждённое имя пользователя, например 'bob'.
-- $3 — ID созданного или найденного пользователя; приложение заполнит его после SELECT.
-- $4 — имя capability выбранного провайдера, например 'auth.login.ldap'.
-- $5 — capability_id создания пользователя из первого RETURNING; нужен только при created=true.
begin;

with created as (
   insert into aid.sys_identity (id, name, kind)
   select
      nextval(pg_get_serial_sequence('aid.app_ident', 'id')),
      $2,
      'user'
   where aid.app_has_permissions(
      $1,
      array[aid.sys_capability_id('identity.user.create')],
      array['can_use']::varchar[]
   )
   -- Имя уникально для всех identity: два одновременных входа не создадут две записи.
   on conflict (name) do nothing
   returning id, aid.sys_capability_id('identity.user.create') as capability_id
), registered as (
   insert into aid.app_ident (id, kind)
   select id, 'sys_identity' from created
)
select id, capability_id from created;

-- Если вернулась строка: created=true; id сохранить в $3, capability_id — в $5; следующий SELECT пропустить.
-- Иначе: created=false; новый SELECT увидит запись после параллельного создания.
select id
from aid.sys_identity
-- Это граница входа: совпадение имени с ролью не позволяет войти от её имени.
where name = $2 and kind = 'user';
-- Если строк нет — ROLLBACK.

-- Только при created=true: начальные права получает создавшая пользователя служба $1.
-- По умолчанию identity.user.create.payload = [], поэтому grants на пользователя не выдаются.
select aid.app_apply_initial_grants($5, $1, $3);

-- Только при created=true: добавить $3 в read запросами раздела «Добавить участника».
-- Применяем их здесь, без вложенных BEGIN/COMMIT: исполнитель $1, участник $3,
-- родитель aid.sys_identity_id('read'). Нужно can_edit службы на read.
-- Роли из role_sources добавляет тот же обработчик проверки членства.
-- Если ролей несколько, сначала блокируем общий список identity по возрастанию ID.
-- Ошибка или отказ при назначении обязательной роли — ROLLBACK всей транзакции.

-- Права на вход могут прийти от read или от других явных grants.
select aid.app_has_permissions(
   $3,
   array[aid.sys_capability_id('auth.login'), aid.sys_capability_id($4)],
   array['can_use', 'can_use']::varchar[]
) as allowed;

-- allowed=false: ROLLBACK, в том числе создания пользователя и его членства.
-- allowed=true: COMMIT, затем выдать пользователю сессию.
commit;
```

Пример: `auth-service` имеет `can_use` на `identity.user.create` и `can_edit` на `read`.
У `read` есть `can_use` на включённые `auth.login` и `auth.login.ldap`.
Параметры: `$1 = aid.sys_identity_id('auth-service')`, `$2 = 'bob'`, `$4 = 'auth.login.ldap'`.

| Ситуация для `bob`                                       | Результат                                                                           |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Пользователя ещё нет                                     | Создаётся одна identity и членство в `read`; проверка входа успешна                 |
| Два одновременных первых входа                           | Один создаёт пользователя и членство; второй читает готовую запись следующим SELECT |
| `auth.login.ldap` выключена                              | Новый пользователь и членство откатываются; сессия не выдаётся                      |
| Удалено членство в `read`, других разрешений на вход нет | Повторный вход запрещён; членство автоматически не возвращается                     |

### Первоначальная настройка каждого приложения

В каждой БД отдельно создаются identity, capabilities с начальными payload и обычные grants ниже.
Их ID выделяются местной `app_ident` и регистрируются в той же транзакции.
Первоначальное наполнение выполняется один раз; рестарт не восстанавливает удалённые права.

| Имя identity               | `kind`    | Назначение                                                        |
| -------------------------- | --------- | ----------------------------------------------------------------- |
| `read`                     | `role`    | Общие права пользователей; назначается при первом создании        |
| `admin`                    | `role`    | Администраторы; входит в `read`, скрытых полномочий нет           |
| `auth-service`             | `service` | Обработчик входа, недоступный для пользовательской аутентификации |
| Имя первого администратора | `user`    | Заранее выбранная учётная запись; входит в `admin`                |

| Получатель     | Объекты                                                             | Начальные `action`                                              |
| -------------- | ------------------------------------------------------------------- | --------------------------------------------------------------- |
| `admin`        | Каждая зарегистрированная capability                                | `can_view`, `can_use`, `can_edit`, `can_grant`, `can_revoke`    |
| `admin`        | Роли `admin` и `read`                                               | `can_view`, `can_edit`, `can_delete`, `can_grant`, `can_revoke` |
| `auth-service` | Capability `identity.user.create`                                   | `can_use`                                                       |
| `auth-service` | Роли, которые ей разрешено назначать; первоначально `admin`, `read` | `can_edit`, `can_delete`                                        |


Общие capabilities для AID, BOBA и MCP. В таблице — grants `can_use` сверх grants `admin` выше:

| `sys_capability.name`                          | Получатель     | Разрешённая операция                                                |
| ---------------------------------------------- | -------------- | ------------------------------------------------------------------- |
| `auth.login`                                   | `read`         | Вход в приложение                                                   |
| `auth.login.<name>`                   | `read`         | Вход через конкретный первоначально настроенный `sys_auth_provider.name` |
| `connection.create`, `auth.create`             | `read`         | Создание connection или credentials                                 |
| `session.create`                               | `read`         | Создание исходящей сессии после проверки доступа к connection/auth  |
| `identity.user.create`                         | `auth-service` | Создание пользователя после проверки credentials                    |
| `identity.role.create`, `identity.team.create` | Только `admin` | Создание роли или команды                                           |

Это grants роли: изменение прав `read` затрагивает всех её участников. Начальные права на создаваемые объекты задаются отдельно в `payload` capabilities.
Отключение личного grant не запрещает право, полученное от `read`.

`sys_*` остаются независимыми от прикладных таблиц. Обработчик входа выбирает
`auth.login.<name>` по имени провайдера. Новый провайдер требует новой capability
и явных grants. Наличие capability не включает провайдер и не заменяет проверку credentials.

Начальный `sys_capability.payload` в каждой БД; каждое имя — отдельная строка:

| `sys_capability.name`                                              | `payload`: права создателя на новый объект                                     |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| `connection.create`, `auth.create`                                 | `["can_view", "can_use", "can_edit", "can_delete", "can_grant", "can_revoke"]` |
| `identity.role.create`, `identity.team.create`                     | `["can_view", "can_edit", "can_delete", "can_grant", "can_revoke"]`            |
| `session.create`                                                   | `["can_view", "can_delete"]`                                                   |
| `identity.user.create`, `auth.login`, `auth.login.<name>` | `[]`                                                                           |

При создании `app_session` проверяются `can_use` на `session.create`, connection и credentials,
если они используются. После создания `app_apply_initial_grants` применяет `session.create.payload`;
получатель — создавшая сессию identity, а не nullable-поле `app_session.identity_id`.
Это исходящая сессия connection, а не сессия входа пользователя в приложение.
Удаление `app_session` удаляет локальный артефакт; отзыв внешнего токена выполняется через API внешней системы.
У `admin` нет автоматического доступа к создаваемым объектам.

## DDL таблиц системы хранения `app_connection`, `app_auth`, `app_session`

Эти таблицы обслуживают connections, которые устанавливает приложение к MCP, LLM и другим внешним ресурсам.

Примеры ниже указываются без шифрования, однако в итоге шифрование секретных полей обязательно

```sql
-- параметры connection к источнику
create table aid.app_connection (
   id          bigint      not null references aid.app_ident (id),
   created_by  bigint      not null references aid.sys_identity (id), -- создавший пользователь
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
-- авторизации с помощью которых можно авторизоваться в connections, описанных в boba.
-- здесь храняться конкретные способы авторизации:
-- kind - указывает тип авторизации
-- payload - все необходимое для совершения авторизации
create table aid.app_auth (
   id          bigint      not null references aid.app_ident (id),
   created_by  bigint      not null references aid.sys_identity (id), -- создавший пользователь
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
-- артефакты после аутентификации в исходящем connection
create table aid.app_session (
   id             bigint      not null references aid.app_ident (id),
   connection_id  bigint      not null references aid.app_connection (id),
   auth_id        bigint      null references aid.app_auth (id),
   identity_id    bigint      null references aid.sys_identity (id), -- identity, от имени которой установлена сессия; не даёт прав на неё
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

В БД AID создаются общие записи из первоначальной настройки и capabilities ниже.
`admin` получает на них общие grants, а `read` — указанные в таблице.

| Capability             | Операция                                                              | Grant роли `read` |
| ---------------------- | --------------------------------------------------------------------- | ----------------- |
| `prompt.create`        | Создать обычный prompt                                                | `can_use`         |
| `system_prompt.create` | Создать system prompt                                                 | Не выдаётся       |
| `skill.create`         | Создать skill                                                         | `can_use`         |
| `memory.create`        | Создать memory; изменение чужого контекста требует `can_edit` на него | `can_use`         |
| `mcp.create`           | Добавить MCP-сервер в реестр                                          | `can_use`         |
| `thread.create`        | Создать thread                                                        | `can_use`         |

| `sys_capability.name`                                   | `payload`: права создателя на новый объект                                     |
| ------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `prompt.create`, `system_prompt.create`, `skill.create` | `["can_view", "can_use", "can_edit", "can_delete", "can_grant", "can_revoke"]` |
| `memory.create`, `mcp.create`, `thread.create`          | `["can_view", "can_use", "can_edit", "can_delete", "can_grant", "can_revoke"]` |

`admin` может выдать роли `read` `can_use` на `system_prompt.create` обычным запросом выдачи grant.
Для общего MCP-сервера создатель выдаёт своей группе `can_view` / `can_use` на сервер
и `can_use` на используемые connection/auth. Адреса и credentials заполняются отдельно.

```sql
-- общие таблицы sys_* используются также и в приложении aid
-- общие таблицы app_* используются также и в приложении aid


-- реестр добавленных mcp серверов.
-- те mcp, которые пользователи видят в своем окружении.
-- это просто список доступных mcp с которыми взаимодействует пользователь.
create table aid.mcp (
   id          bigint      not null references aid.app_ident (id),
   created_by  bigint      not null references aid.sys_identity (id), -- создавший пользователь
   name        varchar     not null check (length(name) > 0),
   description varchar     not null check (length(description) > 0),
   -- здесь храниться остальная информация о connection к mcp
   -- как в app_connection, храниться объект, с помощью которого мы можем сделать сессию
   -- здесь храниться тип авторизации, авторизационные данные если они нужны и прочее
   payload     jsonb       not null check (jsonb_typeof(data) = 'object'),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id)
);
```

Реестры промптов, скилов и памяти, используемых LLM.
grants выдаются на постоянные `id` из `prompt`, `skill`, `memory`.
Версии промптов и скилов — состояния этих объектов, без отдельных `app_ident`.

```sql
-- обычные и системные промпты; любой из них может быть шаблоном
create table aid.prompt (
   id          bigint      not null references aid.app_ident (id),
   created_by  bigint      not null references aid.sys_identity (id),
   -- prompt, system_prompt
   kind        varchar     not null check (length(kind) > 0),
   name        varchar     not null check (length(name) > 0),
   -- текущая версия, указатель на версию в prompt_revision
   revision    integer     not null check (revision > 0),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id),
   unique (created_by, kind, name)
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
   created_by      bigint      not null references aid.sys_identity (id),
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
-- скилы в общем реестре; одинаковые имена допустимы у разных авторов
create table aid.skill (
   id          bigint      not null references aid.app_ident (id),
   created_by  bigint      not null references aid.sys_identity (id),
   -- постоянное имя из SKILL.md, совпадает с именем папки скила
   name        varchar     not null check (length(name) between 1 and 64),
   -- выбранная версия; null, пока версия не выбрана
   revision    integer     null check (revision > 0),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),

   primary key (id),
   unique (created_by, name)
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
   created_by     bigint      not null references aid.sys_identity (id),
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
   created_by bigint      not null references aid.sys_identity (id),
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
create index idx_memory_created_by on aid.memory (created_by);
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
и `expires_at is null or expires_at > now()`. `created_by` и `scope_id`
не заменяют проверку доступа через `sys_grant`.

Чаты пользователей в chainlit
к конкретному чату многое привязывается, 

```sql
create table aid.thread (
   id          bigint      not null references aid.app_ident (id),
   created_by  bigint      not null references aid.sys_identity (id), -- создавший пользователь
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

Общие identity и capabilities с начальными payload создаются в БД BOBA независимо от AID.
`admin` получает общие grants на capabilities ниже; grants `read` указаны в таблице.

| Capability            | Операция                                             | Grant роли `read` |
| --------------------- | ---------------------------------------------------- | ----------------- |
| `thread.create`       | Создать диалог агента                                | `can_use`         |
| `llm_provider.create` | Добавить провайдера инференса                        | Не выдаётся       |
| `llm_model.create`    | Добавить модель; также нужен `can_edit` на провайдер | Не выдаётся       |

| `sys_capability.name`                                      | `payload`: права создателя на новый объект                                     |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `thread.create`, `llm_provider.create`, `llm_model.create` | `["can_view", "can_use", "can_edit", "can_delete", "can_grant", "can_revoke"]` |

Для первого рабочего диалога администратор добавляет хотя бы один провайдер и модель,
а также необходимые connection/auth. На общие провайдер и модель выдаёт роли `read`
`can_view` / `can_use`, на используемые connection/auth — `can_use`.
При инференсе проверяются модель, провайдер и фактически используемые connection/auth
одним списком требований; наличие настроек само по себе разрешения не даёт.
Чтение журнала проверяет `can_view` на thread, продолжение диалога — `can_use` на thread;
на строки journal отдельные grants не выдаются.

```sql
-- общие таблицы app_ident, sys_identity, sys_identity_member, sys_capability,
-- sys_grant, app_connection, app_auth, app_session
-- и таблицы провайдеров входа создаются по описанию выше


-- таблица всех сессий агента, которые когда либо были зарегистрированы
-- created_by хранит автора; доступ любой identity задаётся только grants
create table boba.thread (
   id          bigint      not null references boba.app_ident (id),
   created_by  bigint      not null references boba.sys_identity (id), -- создавший пользователь
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
   created_by     bigint      not null references boba.sys_identity (id), -- создавший пользователь
   name           varchar     not null check(length(name) > 0),
   payload        jsonb       not null check (jsonb_typeof(data) = 'object'),
   -- объект который используется как транспорт connection к llm провайдеру (например http транспорт)
   -- не факт что connection_id вообще нужен для connection, к примеру когда используется локальный onnx
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
   created_by        bigint      not null references boba.sys_identity (id), -- создавший пользователь
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

В БД MCP создаются общие identity и capabilities с начальными payload из общей таблицы выше.
У всех `tool.*.execute` поле `payload` равно `[]`: выполнение инструмента не выдаёт grants на новый объект реестра.

Для каждого установленного плагина регистрируется capability на выполнение его инструментов.
`admin` получает общие grants, а `read` — указанные ниже. Новые плагины требуют явной выдачи прав.

| Capability            | Операция                                   | Grant роли `read` |
| --------------------- | ------------------------------------------ | ----------------- |
| `tool.bash.execute`   | Shell-инструменты в workspace пользователя | `can_use`         |
| `tool.doc.execute`    | Обработка документов                       | `can_use`         |
| `tool.web.execute`    | Web-инструменты                            | `can_use`         |
| `tool.kb.execute`     | Поиск в настроенной базе знаний            | `can_use`         |
| `tool.pg.execute`     | Инструменты работы с БД                    | Не выдаётся       |
| `tool.ingest.execute` | Загрузка данных в базу знаний              | Не выдаётся       |

Обработчик вызова определяет плагин по зарегистрированному инструменту и проверяет
`can_use` на `tool.<plugin>.execute` общей проверкой. Дополнительно включаются требования
на явно выбранные connection/auth. Права внешней БД и ограничения sandbox остаются
условиями выполнения; capability не расширяет их. Workspace выбирается по пользователю.

AID, BOBA и MCP независимо сопоставляют подтверждённого пользователя своим identity.
Между сервисами передаётся проверяемый контекст пользователя; локальные числовые ID
и identity `auth-service` не используются для подмены пользователя в другом сервисе.
Доступ к записи MCP-сервера в AID не заменяет разрешение выполнить инструмент в MCP.
