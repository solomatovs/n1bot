-- Одноразовый перенос истории агента из таблицы прежнего выпуска
-- history_records в таблицы session и session_records той же схемы.
--
-- Прежний выпуск хранил запись целиком документом record, а сессию, seq,
-- вид и время — ещё и колонками или ключами документа. Новый выпуск держит
-- каркас записи колонками history (session_id, seq, type, recorded_at), а в
-- record — только остальное. Скрипт раскладывает каждую строку старой
-- таблицы так же, как пишет хранилище: сессия, seq и вид — из колонок
-- старой строки, время — из ключа timestamp документа, документ — без
-- ключей type, seq, sessionId и timestamp. Строка session заводится на
-- каждую перенесённую сессию со временем её первой записи.
--
-- Перед переносом скрипт проверяет, что ключи документа сходятся с
-- колонками старой строки (sessionId с session_id, seq с seq, type с type) и
-- что время в документе есть: иначе часть записи пропала бы при раскладке, и
-- скрипт останавливается, ничего не изменив, со списком таких строк.
--
-- Сессию, у которой строка в session уже есть (новое приложение успело
-- завести её до переноса), скрипт не трогает и выводит списком. Число
-- перенесённых записей сверяется с числом строк старой таблицы по каждой
-- сессии; при расхождении всё откатывается. Старая таблица не удаляется.
-- Повторный запуск переносить уже нечего: все сессии окажутся в списке
-- пропущенных.
--
-- Запуск (схема по умолчанию boba):
--   PGDATABASE=<база> psql -v schema=boba -f convert-history.sql

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?schema}
\else
    \set schema boba
\endif

select to_regclass(format('%I.history_records', :'schema')) is null as no_old_table \gset
\if :no_old_table
    \echo 'There is no table' :schema'.history_records in this database, so there is nothing to convert.'
    \quit
\endif

begin;

set local client_min_messages = warning;

create temporary table inconsistent on commit drop as
select
    session_id,
    seq,
    type,
    record ->> 'sessionId' as record_session_id,
    record ->> 'seq' as record_seq,
    record ->> 'type' as record_type,
    record ->> 'timestamp' as record_timestamp
from
    :"schema".history_records
where
    record ->> 'sessionId' is distinct from session_id
    or record ->> 'seq' is distinct from seq::text
    or record ->> 'type' is distinct from type
    or record ->> 'timestamp' is null;

select count(*) > 0 as has_inconsistent from inconsistent \gset
\if :has_inconsistent
    \echo 'Rows whose document keys disagree with their columns or lack a timestamp:'
    select * from inconsistent order by session_id, seq limit 50;
    do $$ begin raise exception 'Some rows of history_records cannot be split without losing data (listed above), so nothing was changed.'; end $$;
\endif

create table if not exists :"schema".session (
    id         text        primary key,
    created_at timestamptz not null
);

create table if not exists :"schema".session_records (
    session_id  text        not null references :"schema".session on delete cascade,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);

create temporary table skipped on commit drop as
select distinct
    o.session_id
from
    :"schema".history_records as o
    join :"schema".session as s
        on s.id = o.session_id;

\echo 'Sessions that already have a row in session; they are left as they are:'
select session_id from skipped order by session_id;

create temporary table moved on commit drop as
select
    o.session_id,
    count(*) as records
from
    :"schema".history_records as o
where
    not exists (select 1 from skipped as k where k.session_id = o.session_id)
group by
    o.session_id;

insert into :"schema".session (id, created_at)
select
    o.session_id,
    min((o.record ->> 'timestamp')::timestamptz)
from
    :"schema".history_records as o
    join moved as m
        on m.session_id = o.session_id
group by
    o.session_id;

insert into :"schema".session_records (session_id, seq, type, recorded_at, record)
select
    o.session_id,
    o.seq,
    o.type,
    (o.record ->> 'timestamp')::timestamptz,
    o.record - array['type', 'seq', 'sessionId', 'timestamp']
from
    :"schema".history_records as o
    join moved as m
        on m.session_id = o.session_id
order by
    o.session_id,
    o.seq;

create temporary table mismatched on commit drop as
select
    m.session_id,
    m.records as old_records,
    count(h.seq) as new_records
from
    moved as m
    left join :"schema".session_records as h
        on h.session_id = m.session_id
group by
    m.session_id,
    m.records
having
    count(h.seq) <> m.records;

\echo 'Result of the conversion:'
select
    (select count(*) from :"schema".history_records) as old_rows,
    (select count(*) from skipped) as sessions_skipped,
    (select count(*) from moved) as sessions_converted,
    (select coalesce(sum(records), 0) from moved) as records_expected,
    (
        select
            count(*)
        from
            :"schema".session_records as h
            join moved as m
                on m.session_id = h.session_id
    ) as records_converted;

select count(*) > 0 as failed from mismatched \gset
\if :failed
    \echo 'Sessions whose record count does not match:'
    select session_id, old_records, new_records from mismatched order by session_id;
    do $$ begin raise exception 'The record counts do not match (listed above), so nothing was changed.'; end $$;
\endif

commit;

\echo 'The conversion is committed; the old table history_records is kept.'
