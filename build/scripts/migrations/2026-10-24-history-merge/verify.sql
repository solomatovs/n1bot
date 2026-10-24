-- Сверка истории чата после слияния; ничего не меняет: рабочие временные
-- таблицы живут в транзакции, которая откатывается. Останавливается с
-- ошибкой и списком, если:
--   * тред называет сессию агента (threads.meta.agent_session), а её нет в
--     session или у неё нет ни одной записи в session_records;
--   * запись истории без вида kind, которого читает текущий выпуск, или
--     результат инструмента без исхода status: запись в формате раннего
--     выпуска, агент её не прочтёт;
--   * итог вызова инструмента в записи (toolUseResult) не ответ tools/call:
--     нет массива content.
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

create temporary table unknown_kinds on commit drop as
select
    h.session_id,
    h.seq,
    h.type,
    h.record ->> 'kind' as kind
from
    :"boba_schema".session_records as h
where
    h.record ->> 'kind' is null
    or h.record ->> 'kind' not in (
        'prompt', 'task_notification', 'meta', 'command', 'command_output',
        'tool_result', 'interrupt', 'compact_summary', 'reply', 'no_response',
        'attachment', 'api_error', 'informational', 'compaction',
        'compact_boundary', 'harness_command', 'turn_end', 'queue_operation',
        'permission_mode', 'permission_rule', 'api_request'
    )
    or (
        h.record ->> 'kind' = 'tool_result'
        and coalesce(h.record ->> 'status', '') not in ('completed', 'failed', 'cancelled')
    );

select count(*) > 0 as has_unknown_kinds from unknown_kinds \gset
\if :has_unknown_kinds
    \echo 'Records whose kind this release does not read (no kind, an unknown kind or a tool result without status):'
    select * from unknown_kinds order by session_id, seq limit 50;
    do $$ begin raise exception 'Some history records are in the format of an earlier release (listed above): the agent cannot read them; transfer the history again from dev.'; end $$;
\endif

create temporary table foreign_results on commit drop as
select
    h.session_id,
    h.seq,
    left((h.record -> 'toolUseResult')::text, 200) as result
from
    :"boba_schema".session_records as h
where
    h.record ? 'toolUseResult'
    and jsonb_typeof(h.record -> 'toolUseResult' -> 'content') is distinct from 'array';

select count(*) > 0 as has_foreign from foreign_results \gset
\if :has_foreign
    \echo 'Records whose toolUseResult is not a tools/call result:'
    select * from foreign_results order by session_id, seq limit 50;
    do $$ begin raise exception 'Some tool results are not tools/call results (listed above): transfer the history again from dev.'; end $$;
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
