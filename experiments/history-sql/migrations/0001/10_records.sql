create table if not exists {schema}.records (
    session_id text not null,
    seq bigint not null,
    record_id text not null,
    operation_id text not null,
    operation_position integer not null,
    parent_operation_id text,
    kind text not null,
    category text not null,
    role text,
    scope text,
    key text,
    operation text not null,
    copied_from_id text,
    created_at timestamptz not null,
    version text not null,
    payload jsonb not null,
    primary key (session_id, seq),
    unique (session_id, record_id),
    unique (session_id, operation_id, operation_position)
);

create index if not exists records_category_seq
    on {schema}.records (session_id, category, seq desc);

create index if not exists records_llm_seq
    on {schema}.records (session_id, seq)
    where 1=1
        and category <> 'operational';
