create table if not exists {schema}.mcp_servers (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    data       jsonb not null,
    enabled    boolean not null default true,
    created_at timestamptz not null default now()
);

create unique index if not exists idx_mcp_servers_name
    on {schema}.mcp_servers (name);

comment on table {schema}.mcp_servers is
    'MCP-серверы чата: единственный их источник. Описание сервера (адрес, вход
    и сроки) лежит в data зашифрованными секретами; кому сервер выдан, решают
    строки grants с src_kind mcp_servers. Имя сервера уникально: оно входит в
    полные имена его инструментов и в ключ подключения клиента.';

comment on column {schema}.mcp_servers.name is
    'Имя сервера: латиница, цифры, «_» и «-»; имя chat занято самим чатом.';

comment on column {schema}.mcp_servers.data is
    'Сериализованный StoredServer: endpoint и limits, SecretStr зашифрованы.';

comment on column {schema}.mcp_servers.enabled is
    'Выключенный сервер выдан, но не подключается и в набор агента не попадает.';
