create table if not exists {schema}.agent_user_settings (
    user_id    uuid not null,
    profile    text not null,
    settings   jsonb not null,
    updated_at timestamptz not null default now(),
    primary key (user_id, profile)
);

create table if not exists {schema}.agent_chat_settings (
    thread_id  uuid primary key,
    user_id    uuid not null,
    profile    text not null,
    settings   jsonb not null,
    updated_at timestamptz not null default now()
);
