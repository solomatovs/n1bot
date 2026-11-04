create temporary table pg_temp.users (
    id         uuid primary key default gen_random_uuid(),
    identifier text not null unique,
    created_at timestamptz not null default now(),
    realm      text,
    login      text,
    constraint users_identity check (
        (realm is null and login is null)
        or (realm is not null and login is not null
            and realm ~ '^[a-z0-9][a-z0-9._-]*$'
            and length(login) > 0 and login = lower(btrim(login))
            and identifier = realm || ':' || login)
    )
);

create temporary table pg_temp.roles (
    id        uuid primary key default gen_random_uuid(),
    role      varchar not null unique,
    create_at timestamptz not null default now()
);

create temporary table pg_temp.user_roles (
    user_id uuid not null references pg_temp.users(id) on delete cascade,
    role_id uuid not null references pg_temp.roles(id) on delete cascade,
    managed boolean not null default false,
    primary key (user_id, role_id)
);

create unique index if not exists idx_users_identifier_lower
    on pg_temp.users (lower(identifier));

create index if not exists idx_user_roles_role on pg_temp.user_roles (role_id);

create temporary table pg_temp.grants (
    id          uuid primary key default gen_random_uuid(),
    src_kind    varchar not null,
    src_kind_id uuid not null,
    tgt_kind    varchar not null,
    tgt_kind_id uuid not null,
    unique (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
);

create index if not exists idx_grants_target
    on pg_temp.grants (tgt_kind, tgt_kind_id);

comment on table pg_temp.grants is
    'Shared grants between users and roles and polymorphic resources managed by an application.';

create temporary table pg_temp.threads (
    id         uuid primary key,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    name       text,
    user_id    uuid,
    tags       text[],
    meta       jsonb
);

create index if not exists idx_threads_user_id
    on pg_temp.threads (user_id);

create index if not exists idx_threads_user_updated
    on pg_temp.threads (user_id, updated_at desc, id desc);

create temporary table pg_temp.feedbacks (
    id        uuid primary key,
    for_id    uuid not null,
    value     smallint not null,
    thread_id uuid,
    comment   text
);

create index if not exists idx_feedbacks_for_id
    on pg_temp.feedbacks (for_id);

create temporary table pg_temp.live_instances (
    instance_id  text primary key,
    app          text not null check (app in ('aid')),
    host         text not null,
    started_at   timestamptz not null default now(),
    heartbeat_at timestamptz not null default now()
);

create temporary table pg_temp.live_events (
    scope_kind text not null,
    scope_id   uuid not null,
    seq        bigint not null,
    kind       text not null,
    origin     text not null,
    body       jsonb not null,
    at         timestamptz not null default now(),
    primary key (scope_kind, scope_id, seq)
);

create temporary table pg_temp.live_commands (
    id          bigint generated always as identity primary key,
    scope_kind  text not null,
    scope_id    uuid not null,
    action      text not null check (action in ('stop')),
    body        jsonb not null,
    by_instance text not null,
    at          timestamptz not null default now(),
    taken_by    text,
    taken_at    timestamptz
);

create index if not exists idx_live_commands_scope
    on pg_temp.live_commands (scope_kind, scope_id);

create temporary table pg_temp.live_payloads (
    scope_kind text not null,
    scope_id   uuid not null,
    id         uuid primary key,
    body       json not null,
    at         timestamptz not null default now()
);

create index if not exists idx_live_payloads_scope
    on pg_temp.live_payloads (scope_kind, scope_id);

create temporary table pg_temp.live_locks (
    scope_kind   text not null,
    scope_id     uuid not null,
    mode         text not null check (mode in ('exclusive', 'shared')),
    holder       text not null
        references pg_temp.live_instances (instance_id) on delete cascade,
    token        uuid not null,
    purpose      text not null check (purpose in ('turn', 'run', 'tool_call', 'cleanup')),
    user_id      uuid not null,
    acquired_at  timestamptz not null default now(),
    heartbeat_at timestamptz not null default now(),
    ttl_sec      integer not null check (ttl_sec > 0),
    primary key (scope_kind, scope_id, token)
);

create index if not exists idx_live_locks_scope
    on pg_temp.live_locks (scope_kind, scope_id);

create temporary table pg_temp.mcp_servers (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    data       jsonb not null,
    enabled    boolean not null default true,
    created_at timestamptz not null default now()
);

create unique index if not exists idx_mcp_servers_name
    on pg_temp.mcp_servers (name);

comment on table pg_temp.mcp_servers is
    'MCP-серверы чата: единственный их источник. Описание сервера (адрес, вход
    и сроки) лежит в data зашифрованными секретами; кому сервер выдан, решают
    строки grants с src_kind mcp_servers. Имя сервера уникально: оно входит в
    полные имена его инструментов и в ключ подключения клиента.';

comment on column pg_temp.mcp_servers.name is
    'Имя сервера: латиница, цифры, «_» и «-»; имя chat занято самим чатом.';

comment on column pg_temp.mcp_servers.data is
    'Сериализованный StoredServer: endpoint и limits, SecretStr зашифрованы.';

comment on column pg_temp.mcp_servers.enabled is
    'Выключенный сервер выдан, но не подключается и в набор агента не попадает.';

create temporary table pg_temp.mcp_tokens (
    server_id uuid not null references pg_temp.mcp_servers(id) on delete cascade,
    user_id uuid not null references pg_temp.users(id) on delete cascade,
    token_data text,
    client_data text,
    updated_at timestamptz not null default now(),
    primary key (server_id, user_id)
);

comment on table pg_temp.mcp_tokens is
    'OAuth credentials for one MCP server and one AID user. token_data and client_data are encrypted with SecretCipher; pending authorization codes are never stored here.';
