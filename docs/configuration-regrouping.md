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
- sys_oauth_client - доверенные приложения oauth которые проходят через proxy авторизацию

При старте приложение читает активные настройки для работы сервиса авторизации и собирает их в памяти. Далее приложение не читает таблицы, все необходимое храниться в памяти до момента когда придет сигнал SIGHUP который запускает обновление конфига и прочего. В этот момент системные таблицы перечитываются и начинают действовать новые настройки.


```sql
-- здесь я использую схему aid для примера
-- те же таблицы будут в приложениях boba и mcp
-- так как система основа у них общая
create schema aid;

-- провайдеры авторизации
create table aid.sys_auth_provider (
   name           varchar     primary key,
   kind           varchar     not null, -- local, ldap, kerberos, proxy
   -- полезная нагрузка описывающая подключение к провайдеру авторизации. К примеру подключение к ldap
   payload        jsonb       not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
   -- провайдеры ролей из таблицы sys_role_provider. Перечисление в виде массива
   role_providers jsonb       not null default '[]'::jsonb check (jsonb_typeof(credential) = 'array'),
   enable         boolean     not null default true,
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now()
);
```

Примеры строк представлены в виде json для удобства. Обрати внимание, что секретная часть должна быть зашифрована, механизм шифрования уже разработан в коде и его надо использовать для секретной части. Для удобства в примерах нет шифрования.

Обрати внимание что примеры ниже отображают те модели, которые уже есть в коде с той разницей, что они перенесены в базу данных, в отдельные поля. Однако же, сейчас система авторизации в целом готова, и требуется в основном сменить источник ее конфигураций.

Так или иначе, в примерах описана целевая форма конфигурирования к которой я стремлюсь и нужно постараться найти несоответствия с ней, указать мне и вместе решить что с этим делать.

Так мог бы выглядеть `payload` для разных провайдеров авторизации:

```json
{
   "name": "local_users",
   "kind": "local",
   "payload": {},
   "payload": {
      "users": {
         "local_user": "$argon2id$..."
      },
   },
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
}
```

Keytab здесь указан как файл на файловой системе.
```json
{
   "name": "kerberos",
   "kind": "kerberos",
   "payload": {
      "spn": "HTTP/mcp.example.org@EXAMPLE.ORG",
      "principal_format": "{username}@EXAMPLE.ORG",
      "keytab": "krb/mcp.keytab"
   },
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
      },
      "secret": "..."
   },
}
```


```sql
-- источники ролей со своими правилами и параметрами
create table aid.sys_role_provider (
   name        varchar     primary key,
   kind        varchar     not null, -- local, ldap, principal, proxy
   payload     jsonb       not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
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
      ],
      "connection": {
         "bind": {
            "method": "simple",
            "user": "CN=lookup,OU=Services,DC=example,DC=org",
            "password": "..."
         },
      },
   },
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
}
```

```json
{
   "name": "proxy",
   "kind": "proxy",
   "payload": {
      "header": "X-Remote-Roles"
   },
}
```


```sql
-- одно доверенное приложение из auth.oauth.clients
create table aid.sys_oauth_client (
   client_id   varchar     primary key,
   payload     jsonb       not null check (jsonb_typeof(credential) = 'object'),
   enable      boolean     not null default true,
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now()
);
```

Пример отдельной строки OAuth-клиента:

```json
{
   "client_id": "aid",
   "payload": {
      "secret": "..."
   }
}
```

Секрет OAuth-клиента проверяет приложение, а секрет proxy-провайдера —
его утверждение о пользователе. В БД непустой `credential` шифруется целиком:
`{"ciphertext":"<fernet>"}`.


## DDL таблиц приложения (общие таблицы для приложений aid, boba, mcp)

Префикс `sys_` используется для общих таблиц приложений aid, boba, mcp
Ниже используется схема `aid`, но в приложениях boba, mcp будет использоваться собственная.
В основном здесь будет описываться система ролевого доступа к объектам приложения.
Любой объект в системе имеет свой адрес, состоящий из: <таблица> и <id> записи.

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
create unique uk_sys_identity_name on aid.sys_identity(name);


-- Таблица сессий пользователей
-- таблица используется для хранения сессии пользователей, а также метаданных этих сессий, которые с этой сессией ассоциированы
-- таблица является единственным источником информации о том, есть ли сессия, сколько времени она еще будет работать и прочее, что требуется авторизации
create table aid.sys_identity_session (
   id                bigserial   not null,
   -- identity, от имени которой установлена сессия; не даёт прав на неё
   identity_id       bigint      null references aid.sys_identity (id),
   -- провайдер, который авторизовал пользователя
   auth_provider_id  bigint      null references aid.sys_auth_provider (id),
   -- полезная нагрузка для хранения информации о текущей сессии. То, что может быть провайдер специфично
   payload           jsonb       not null check (jsonb_typeof(payload) = 'object'),
   -- время экспирации для этой сессии, вычисляется в процессе установки сессии
   -- проверяется когда необходимо удалить истекшие сессии, либо когда проверяется валидность сессии
   expires_at,       timestamptz not null,
   created_at        timestamptz not null default now(),
   updated_at        timestamptz not null default now(),

   primary key (id)
);
create unique uk_sys_identity_session on aid.sys_identity(identity_id);


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
   -- - creator - объект, который воспользовался capability и создал объект
   -- - new - объект, который был создан тем, кто воспользовался capability
   recipient   varchar     not null check (recipient in ('creator', 'new')),
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
( "connection_create", "creator", [ "can_use", "can_edit", "can_read", "can_delete", "can_grant" ], "Права для владельца конекшина" );
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
Набор прав по умолчанию хранится в `sys_default_grant` для конкретного именованного действия. Именованое действие используется в приложении и по нему производиться поиск прав которые нужно раздать на объект, после создания объекта.
Например, `connection.create.payload` перечисляет права создателя на новый connection.
Пустой массив `[]` означает, что первоначальные grants не выдаются.

## Целевая ddl схема агента BOBA

```sql
-- общие таблицы используются в агенте boba полностью: sys_*


-- Таблица пользователей приложения
-- Все пользователи это исключительно identity, с некоторым набором метаданных из приложения boba
-- кол-во метаданных пока не определено и его нужно расширять
create table boba.user (
   identity_id    bigint      not null references aid.sys_identity (id),
   display_name   varchar     not null,
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),

   primary_key (identity_id)
);
create unique uk_sys_identity_id on aid.sys_identity(identity_id);

-- Таблица ролей пользователей
-- Все роли это те же identity
-- роли пользователей обновляются каждую авторизацию и собираются из того, что предоставили role_provider'ы
create table boba.role (
   identity_id    bigint      not null,
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),

   primary_key (identity_id)
)

-- таблица всех чатов
create table boba.thread (
   id             bigserial   not null,
   -- пользователь к которому привязан провайдер
   identity_id    bigint      not null references aid.sys_identity (id),
   -- сессии можно дать любое имя
   -- acp протокол генерирует название и заполняет его
   name           varchar     not null check (length(name) > 0),
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),
   
   primary key(id)
);
create index idx_threads_updated_name on boba.thread(updated_at desc, name);

-- таблица истории, которую ведет агент в собственно формате
-- Эта история преобразуется в формат acp и отдается по протоколу acp
-- проприетарная история в будущем позволит реализовать поддержку других протоколов, напирмер openai
-- и это нужно будет проверить
create table boba.thread_history (
   id          bigserial   not null,
   -- к какому thread привязано сообщение
   thread_id   bigint      not null references thread (id),
   -- последовательность сообщений для thread_id
   seq         integer     not null,
   -- тип данных внутри payload
   kind        varchar     not null check(length(kind) > 0)
   -- поле хранит итоговую нагрузку. ранее это поле называлось record
   payload     jsonb       not null check (jsonb_typeof(data) = 'object'),
   created_at  timestamptz not null default now(),
   updated_at  timestamptz not null default now(),
   
   primary key (thread_id, seq desc)
);
create index idx_thread_history_thread_id_updated_at on boba.journal(thread_id, updated_at desc);


-- реестр llm провайдеров с которыми работает boba
-- llm провайдер это объект который знает как подключиться к llm инференсу
-- llm провайдер хранит данные о том, какой протокол общения используется в провайдере
-- сейчас в коде уже поддерживаются 3 варианта:
-- openai - openai совместимый провайдер
-- onnx - onnx локальный инференс (в основном для маленьких моделей)
-- ollama - ollama совместимый провайдер
-- fastembeding - не уверен что это отдельный тип провайдера (надо проверить)
-- payload должен содержать сериализованые данные подключения и учетку
-- только эти llm провайдеры можно использовать для подключения к конкретным llm
create table boba.llm_provider (
   id             bigserial   not null,
   -- имя провайдера
   name           varchar     not null check(length(name) > 0),
   -- объект который описывает полностью подключение к llm провайдеру, в том числе и credentials
   connection     jsonb       not null check (jsonb_typeof(data) = 'object'),
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),

   primary key (id)
);

-- llm модели, которые доступны в llm provider'ах
-- обновление списка моделей - ответственность администратора системы
-- приложение читает из этой таблицы списки, для того, что бы возвращать пользователю как доступные м одели того или инного провайдера
create table boba.llm_provider_model (
   id                bigserial   not null,
   -- провайдер к которому привязана модель
   llm_provider_id   bigint      not null references boba.llm_provider (id),
   -- имена моделей
   name              varchar     not null check(length(name) > 0),
   -- 
   connection     jsonb       not null check (jsonb_typeof(data) = 'object'),
   created_at     timestamptz not null default now(),
   updated_at     timestamptz not null default now(),

   primary key (id)
);
```

вот предлагаемый вариант хранения payload для различных провайдеров. 
смысл поля в том что бы дать свободу в том, какие параметры мы здесь храним и какие параметры можем переопределять
схема самостоятельно описывает какие именно параметры доступы, какие у них типы и варианты значений
а пользователь фактически будет выбирать из указанных параметров
defaults будут применяться там где нет явно переданного значения от пользователя
вот мой пример:
```json
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


наполнение таблицы возможностей - capability
```sql
insert into boba.sys_capability (name, description) values
( "agent.session", "возможность работы с сессиями" )
;
```

гранты по умолчанию
```sql
insert into sys_default_grant(name, recipient, grant, description) values
-- thread_create - событие создания нового thread
( "thread_create", "creator", [ "can_use", "can_edit", "can_read", "can_delete", "can_grant" ], "Права для владельца thread" ),
```