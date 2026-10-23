-- Таблицы истории агента в раскладке выпуска 2026-10-16: хранилище
-- PostgresHistoryStore (boba-history-postgres) доводит их на месте до своей,
-- это проверяет тест хранилища. Файл выполняется из migrate.sql с search_path
-- на схеме истории.
create table if not exists session (
    id         text        primary key,
    created_at timestamptz not null
);

create table if not exists history (
    session_id  text        not null references session on delete cascade,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);
