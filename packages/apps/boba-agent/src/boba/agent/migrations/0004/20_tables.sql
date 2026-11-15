create temporary table pg_temp.users (
    id         uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    login      text not null,
    constraint users_login_key unique (login),
    constraint users_identity check (
        length(login) > 0 and login = lower(btrim(login))
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

create index if not exists idx_user_roles_role on pg_temp.user_roles (role_id);

-- One logical message per row; ordered content is stored in payload.content.
create temporary table pg_temp.records (
    session_id text not null,
    seq bigint not null,
    record_id text not null,
    kind text not null,
    category text not null,
    role text,
    key text,
    operation text not null,
    request_id text,
    copied_from_id text,
    created_at timestamptz not null,
    version text not null,
    payload jsonb not null,
    primary key (session_id, seq)
);

create unique index records_id
    on pg_temp.records (session_id, record_id);

create index records_category_seq
    on pg_temp.records (session_id, category, seq desc);

-- Exclude operational records when reading the active context.
create index records_llm_seq
    on pg_temp.records (session_id, seq)
    where 1=1
        and category <> 'operational';

-- ACP lookup and replay provenance; kind remains a root SQL field.
create index records_kind_seq
    on pg_temp.records (session_id, kind, seq desc);

create index records_copies_seq
    on pg_temp.records (session_id, seq)
    where 1=1
        and copied_from_id is not null;

create index records_elicitation
    on pg_temp.records (session_id, (payload ->> 'elicitation_id'), kind)
    where 1=1
        and kind in ('elicitation_response', 'elicitation_completed');

-- A received message enters the LLM dialogue once; compaction copies use fresh IDs.
create unique index records_admission
    on pg_temp.records (session_id, (payload ->> 'source_record_id'))
    where 1=1
        and category = 'admission';

create unique index records_permission_response
    on pg_temp.records (session_id, (payload ->> 'permission_request_id'))
    where 1=1
        and kind = 'permission_response';

-- Ownership metadata; all history content is stored only in records.
create temporary table pg_temp.sessions (
    session_id text primary key,
    owner_id text not null,
    cwd text not null
);

create index sessions_owner_id on pg_temp.sessions (owner_id, session_id);

create temporary table pg_temp.providers (
    id uuid primary key default gen_random_uuid(),
    owner_id text,
    provider_id text not null,
    api_type text not null,
    enabled boolean not null,
    base_url text not null,
    configuration jsonb not null,
    updated_at timestamptz not null default now(),
    unique nulls not distinct (owner_id, provider_id)
);
