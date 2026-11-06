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

create temporary table pg_temp.connections (
    id   uuid primary key default gen_random_uuid(),
    name text not null,
    data jsonb not null default '{{}}'::jsonb
);

create index if not exists idx_connections_kind
    on pg_temp.connections ((data ->> 'kind'));

create temporary table pg_temp.mcp_endpoints (
    id uuid primary key default gen_random_uuid(),
    prefix_url text not null,
    title text not null,
    description text not null default '',
    instructions text not null default '',
    enabled boolean not null default true,
    tools jsonb not null default '[]',
    overrides jsonb not null default '{{}}',
    prompts jsonb not null default '{{}}',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint mcp_endpoints_path check (
        (prefix_url = '/' or prefix_url ~ '^(/[A-Za-z0-9_~.-]+)+$')
        and prefix_url !~ '(^|/)\.\.?(/|$)'),
    constraint mcp_endpoints_tools_array check (jsonb_typeof(tools) = 'array'),
    constraint mcp_endpoints_overrides_object check (jsonb_typeof(overrides) = 'object'),
    constraint mcp_endpoints_prompts_object check (jsonb_typeof(prompts) = 'object')
);

create unique index idx_mcp_endpoints_prefix_url on pg_temp.mcp_endpoints (prefix_url);
