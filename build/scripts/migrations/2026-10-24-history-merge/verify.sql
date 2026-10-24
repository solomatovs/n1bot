-- Сверка истории чата после слияния; ничего не меняет: рабочие временные
-- таблицы живут в транзакции, которая откатывается. Останавливается с
-- ошибкой и списком, если:
--   * тред называет сессию агента (threads.meta.agent_session), а её нет в
--     session или у неё нет ни одной записи в session_records;
--   * итог вызова инструмента в записи (toolUseResult) не ответ tools/call
--     (нет массива content) или у перенесённого итога его нет вовсе —
--     normalize.sql его не привёл;
--   * в старой таблице history_records, если она ещё есть, у сессии больше
--     записей, чем в session_records: часть истории не перенесена.
-- Затем выводит итог: треды, треды с сессией и без неё, сессии, записи,
-- сессии без владельца и сессии, которых не называет ни один тред.
--
-- Переменные psql (-v), необязательные: aid_schema (aid), boba_schema (boba).
--
-- Запуск:
--   PGDATABASE=<целевая база> psql -f verify.sql

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?aid_schema}
\else
    \set aid_schema aid
\endif
\if :{?boba_schema}
\else
    \set boba_schema boba
\endif

begin;

create temporary table lost_sessions on commit drop as
select
    t.id as thread_id,
    t.meta ->> 'agent_session' as session_id,
    s.id is not null as has_session_row
from
    :"aid_schema".threads as t
    left join :"boba_schema".session as s
        on s.id = t.meta ->> 'agent_session'
where
    t.meta ? 'agent_session'
    and not exists (
        select 1
        from :"boba_schema".session_records as h
        where h.session_id = t.meta ->> 'agent_session'
    );

select count(*) > 0 as has_lost from lost_sessions \gset
\if :has_lost
    \echo 'Threads whose agent session has no history:'
    select * from lost_sessions order by thread_id limit 50;
    do $$ begin raise exception 'Some threads name an agent session without history (listed above).'; end $$;
\endif

create temporary table foreign_results on commit drop as
select
    h.session_id,
    h.seq,
    left((h.record -> 'toolUseResult')::text, 200) as result
from
    :"boba_schema".session_records as h
where
    (
        h.record ? 'toolUseResult'
        and jsonb_typeof(h.record -> 'toolUseResult' -> 'content') is distinct from 'array'
    )
    or (
        not h.record ? 'toolUseResult'
        and h.record ->> 'version' = 'history-migrate-2026-10'
        and jsonb_typeof(h.record -> 'message' -> 'content') = 'array'
        and exists (
            select 1
            from jsonb_array_elements(h.record -> 'message' -> 'content') as b(block)
            where b.block ->> 'type' = 'tool_result'
        )
    );

select count(*) > 0 as has_foreign from foreign_results \gset
\if :has_foreign
    \echo 'Records whose toolUseResult is not a tools/call result:'
    select * from foreign_results order by session_id, seq limit 50;
    do $$ begin raise exception 'Some tool results are not tools/call results (listed above): run normalize.sql.'; end $$;
\endif

select to_regclass(format('%I.history_records', :'boba_schema')) is not null as has_old_table \gset
\if :has_old_table
    create temporary table short_sessions on commit drop as
    select
        o.session_id,
        count(*) as old_records,
        (select count(*) from :"boba_schema".session_records as h where h.session_id = o.session_id) as new_records
    from
        :"boba_schema".history_records as o
    group by
        o.session_id
    having
        count(*) > (select count(*) from :"boba_schema".session_records as h where h.session_id = o.session_id);

    select count(*) > 0 as has_short from short_sessions \gset
    \if :has_short
        \echo 'Sessions of history_records with fewer records in history:'
        select * from short_sessions order by session_id;
        do $$ begin raise exception 'Some sessions of history_records are not converted (listed above): run convert-history.sql.'; end $$;
    \endif
\endif

\echo 'Chat history of the database:'
select
    (select count(*) from :"aid_schema".threads) as threads,
    (select count(*) from :"aid_schema".threads where meta ? 'agent_session') as threads_with_session,
    (select count(*) from :"aid_schema".threads where not coalesce(meta ? 'agent_session', false)) as threads_without_session,
    (select count(*) from :"boba_schema".session) as sessions,
    (select count(*) from :"boba_schema".session_records) as records,
    (select count(*) from :"boba_schema".session where owner is null) as sessions_without_owner,
    (
        select count(*)
        from :"boba_schema".session as s
        where not exists (select 1 from :"aid_schema".threads as t where t.meta ->> 'agent_session' = s.id)
    ) as sessions_without_thread;

rollback;

\echo 'The history is consistent.'
