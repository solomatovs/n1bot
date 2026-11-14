create schema history_sql;

-- One logical message per row; ordered content is stored in payload.content.
create table history_sql.records (
    session_id text not null,
    seq bigint not null,
    record_id text not null,
    created_at timestamptz not null,
    version text,
    kind text not null,
    category text not null,
    key text,
    operation text,
    request_id text,
    payload jsonb not null,
    primary key (session_id, seq)
);

create unique index records_id
    on history_sql.records (session_id, record_id);

create index records_category_seq
    on history_sql.records (session_id, category, seq desc);

-- Exclude operational records when reading the active context.
create index records_llm_seq
    on history_sql.records (session_id, seq)
    where 1=1
        and category <> 'operational';
