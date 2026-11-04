create table if not exists {schema}.mcp_tokens (
    server_id uuid not null references {schema}.mcp_servers(id) on delete cascade,
    user_id uuid not null references {schema}.users(id) on delete cascade,
    token_data text,
    client_data text,
    updated_at timestamptz not null default now(),
    primary key (server_id, user_id)
);

comment on table {schema}.mcp_tokens is
    'OAuth credentials for one MCP server and one AID user. token_data and client_data are encrypted with SecretCipher; pending authorization codes are never stored here.';
