-- Четвёртый шаг слияния: записи истории агента, разложенные из старой
-- таблицы history_records (convert-history.sql), приводятся к виду, который
-- читает текущий выпуск, и сессиям тредов чата назначается владелец.
--
-- Итог вызова инструмента в записи (toolUseResult) текущий выпуск читает
-- как ответ tools/call сервера MCP (CallToolResult: массив content, isError,
-- structuredContent). Прежние выпуски писали его иначе, и скрипт переводит
-- три таких вида:
--   * wrapped — ранний агент нового чата клал ответ tools/call под ключ
--     artifact; ответ достаётся из-под ключа как есть;
--   * question — ранний агент писал итог вопроса пользователю
--     (AskUserQuestion: questions, answers, picks, outcome) голым объектом;
--     он становится structuredContent ответа, текстом ответа — текст блока
--     tool_result записи, isError — пометка этого блока, как пишет агент
--     сейчас;
--   * dev — перенос из dev до 2026-10-23 клал результат dev (artifact вида
--     kind/payload), адрес журнала вызова (journal) и типы аргументов
--     (media); ответ строится так же, как его строит migrate.sql перехода
--     2026-10-16 сейчас: текст блока tool_result одним блоком text, ссылки
--     resource_link на каналы tool_stdout и tool_stderr журнала по его
--     адресу, isError — пометка блока; результат dev и типы аргументов не
--     переносятся.
--   * bare — тот же перенос не писал toolUseResult вовсе, когда у результата
--     dev не было ни артефакта, ни адреса журнала; такая запись помечена
--     версией переноса history-migrate-2026-10, и ответ строится так же,
--     как для dev, без ссылок журнала. Запись агента без toolUseResult —
--     вызов, оборванный концом хода, — не трогается.
-- Запись другого вида, запись без ровно одного блока tool_result с текстом
-- строкой и адрес журнала не из букв, цифр и «_.~-» останавливают скрипт до
-- изменений со списком таких записей.
--
-- Владелец сессии (session.owner) — логин пользователя треда, который
-- называет сессию ключом threads.meta.agent_session; сессия без владельца не
-- видна пользователям службы агента. Назначается только сессиям, у которых
-- владельца ещё нет.
--
-- Время треда (threads.updated_at, если его уже завёл чат текущего
-- выпуска) сдвигается вперёд ко времени последней записи его сессии: старт
-- чата заводит колонку временем создания треда, и без сдвига список тредов
-- шёл бы по созданию, а не по последней беседе.
--
-- Повторный запуск ничего не меняет: записи уже в текущем виде, владельцы
-- назначены, время тредов сдвинуто.
--
-- Переменные psql (-v), необязательные: aid_schema (aid), boba_schema (boba).
--
-- Запуск:
--   PGDATABASE=<целевая база> psql -f normalize.sql

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

select to_regclass(format('%I.session_records', :'boba_schema')) is null as no_history \gset
\if :no_history
    \echo 'There is no table' :boba_schema'.session_records in this database: convert or merge the history first.'
    do $$ begin raise exception 'Nothing to normalize without the agent history tables, so nothing was changed.'; end $$;
\endif

begin;

set local client_min_messages = warning;

alter table :"boba_schema".session add column if not exists owner text;

create index if not exists session_owner_id_idx on :"boba_schema".session (owner, id);

create temporary table result_rows on commit drop as
select
    h.session_id,
    h.seq,
    h.record -> 'toolUseResult' as result,
    case
        when not h.record ? 'toolUseResult'
            then 'bare'
        when jsonb_typeof(h.record -> 'toolUseResult' -> 'content') = 'array'
            then 'current'
        when (select array_agg(key) from jsonb_object_keys(h.record -> 'toolUseResult') as keys(key)) = array['artifact']
            and jsonb_typeof(h.record -> 'toolUseResult' -> 'artifact' -> 'content') = 'array'
            then 'wrapped'
        when h.record -> 'toolUseResult' ?& array['questions', 'outcome']
            then 'question'
        when (select array_agg(key) from jsonb_object_keys(h.record -> 'toolUseResult') as keys(key)) <@ array['artifact', 'journal', 'media']
            and coalesce(h.record -> 'toolUseResult' -> 'artifact' ? 'kind', true)
            then 'dev'
        else 'unknown'
    end as form,
    (
        select count(*)
        from jsonb_array_elements(h.record -> 'message' -> 'content') as b(block)
        where b.block ->> 'type' = 'tool_result'
    ) as blocks,
    (
        select b.block
        from jsonb_array_elements(h.record -> 'message' -> 'content') as b(block)
        where b.block ->> 'type' = 'tool_result'
        limit 1
    ) as block
from
    :"boba_schema".session_records as h
where
    jsonb_typeof(h.record -> 'message' -> 'content') = 'array'
    and (
        jsonb_typeof(h.record -> 'toolUseResult') = 'object'
        or (
            not h.record ? 'toolUseResult'
            and h.record ->> 'version' = 'history-migrate-2026-10'
            and exists (
                select 1
                from jsonb_array_elements(h.record -> 'message' -> 'content') as b(block)
                where b.block ->> 'type' = 'tool_result'
            )
        )
    );

create temporary table unconvertible on commit drop as
select
    session_id,
    seq,
    form,
    blocks,
    left(result::text, 200) as result
from
    result_rows
where
    form = 'unknown'
    or (
        form in ('question', 'dev', 'bare')
        and (
            blocks <> 1
            or jsonb_typeof(block -> 'content') <> 'string'
        )
    )
    or (
        form = 'dev'
        and result ? 'journal'
        and not (
            (select array_agg(key order by key) from jsonb_object_keys(result -> 'journal') as keys(key)) = array['call', 'run', 'server']
            and result -> 'journal' ->> 'run' ~ '^[A-Za-z0-9_.~-]+$'
            and result -> 'journal' ->> 'call' ~ '^[A-Za-z0-9_.~-]+$'
        )
    );

select count(*) > 0 as has_unconvertible from unconvertible \gset
\if :has_unconvertible
    \echo 'Records whose toolUseResult cannot be brought to a tools/call result:'
    select * from unconvertible order by session_id, seq limit 50;
    do $$ begin raise exception 'Some tool results of the history are of an unknown form (listed above), so nothing was changed.'; end $$;
\endif

create temporary table converted on commit drop as
select
    session_id,
    seq,
    form,
    case form
        when 'wrapped' then result -> 'artifact'
        when 'question' then jsonb_build_object(
            'content', jsonb_build_array(jsonb_build_object('type', 'text', 'text', block ->> 'content')),
            'isError', coalesce((block ->> 'is_error')::boolean, false),
            'resultType', 'complete',
            'structuredContent', result
        )
        else jsonb_build_object(
            'content', jsonb_build_array(jsonb_build_object('type', 'text', 'text', block ->> 'content'))
                || coalesce(
                    (
                        select
                            jsonb_agg(
                                jsonb_build_object(
                                    'type', 'resource_link',
                                    'uri', 'boba-journal://' || (result -> 'journal' ->> 'run') || '/'
                                        || (result -> 'journal' ->> 'call') || '/' || channel,
                                    'name', channel,
                                    'mimeType', 'text/plain',
                                    'annotations', jsonb_build_object('audience', jsonb_build_array('user'))
                                )
                                order by ord
                            )
                        from
                            unnest(array['tool_stdout', 'tool_stderr']) with ordinality as channels(channel, ord)
                        where
                            result ? 'journal'
                    ),
                    '[]'::jsonb
                ),
            'isError', coalesce((block ->> 'is_error')::boolean, false)
        )
    end as result
from
    result_rows
where
    form in ('wrapped', 'question', 'dev', 'bare');

update :"boba_schema".session_records as h
set
    record = jsonb_set(h.record, '{toolUseResult}', c.result)
from
    converted as c
where
    h.session_id = c.session_id
    and h.seq = c.seq;

create temporary table owned (id text) on commit drop;

with assigned as (
    update :"boba_schema".session as s
    set
        owner = u.identifier
    from
        :"aid_schema".threads as t
        join :"aid_schema".users as u
            on u.id = t.user_id
    where
        t.meta ->> 'agent_session' = s.id
        and s.owner is null
    returning s.id
)
insert into owned select id from assigned;

create temporary table moved_threads (id uuid) on commit drop;

select exists (
    select 1
    from information_schema.columns
    where table_schema = :'aid_schema'
        and table_name = 'threads'
        and column_name = 'updated_at'
) as has_updated_at \gset
\if :has_updated_at
    with lasts as (
        select
            h.session_id,
            max(h.recorded_at) as last
        from
            :"boba_schema".session_records as h
        group by
            h.session_id
    ),
    moved as (
        update :"aid_schema".threads as t
        set
            updated_at = l.last
        from
            lasts as l
        where
            l.session_id = t.meta ->> 'agent_session'
            and l.last > t.updated_at
        returning t.id
    )
    insert into moved_threads select id from moved;
\endif

\echo 'Tool results brought to a tools/call result, by their former form:'
select form, count(*) as records from converted group by form order by form;

\echo 'Sessions that got the login of their thread user as the owner:'
select count(*) as sessions from owned;

\echo 'Threads whose updated_at moved to the last record of their session:'
select count(*) as threads from moved_threads;

commit;

\echo 'The normalization is committed.'
