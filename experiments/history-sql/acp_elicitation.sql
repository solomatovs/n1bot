-- Запрос действия у клиента: одна elicitation_request -> вызов ACP elicitation/create.
-- Вызывается после сохранения вопроса; восстановление истории его не запускает.
-- Ручной просмотр: в params подставить 'sql-acp-demo', 'pending-form' и выполнить весь файл.
-- 'url' выбирает внешнее действие; 'form' уже отвечен и будет отклонён валидатором.
-- Результат: method, params и данные проверки. RPC id и отправку обеспечивает транспорт.
-- Подробнее: docs/agent-history-to-acp.md, раздел «Как использовать запросы».
with
params as (
    select
        $1::text as session_id,       -- records.session_id: чат, в котором задаётся вопрос
        $2::text as record_id         -- records.record_id самой elicitation_request
),
history as not materialized (
    select
        r.*
    from
        history_sql.records r
    where 1=1
        and r.session_id = (select session_id from params)
),
request as (
    select
        h.seq,
        h.record_id,
        h.payload
    from
        history h
        cross join params p
    where 1=1
        and h.record_id = p.record_id
        and h.kind = 'elicitation_request'
        and h.copied_from_id is null
),
responses as (
    select
        count(*) as count
    from
        history h
        cross join params p
    where 1=1
        and h.kind = 'elicitation_response'
        and h.payload ->> 'elicitation_id' = p.record_id
),
completions as (
    select
        count(*) as count
    from
        history h
        cross join params p
    where 1=1
        and h.kind = 'elicitation_completed'
        and h.payload ->> 'elicitation_id' = p.record_id
),
variants as (
    select
        jsonb_build_object('requestedSchema', payload -> 'requested_schema') as fields
    from
        request
    where 1=1
        and payload ->> 'mode' = 'form'
    union all
    select
        jsonb_build_object('url', payload -> 'url', 'elicitationId', record_id)
    from
        request
    where 1=1
        and payload ->> 'mode' = 'url'
)
select
    p.session_id,
    p.record_id,
    r.seq,
    a.count as response_count,
    c.count as completion_count,
    'elicitation/create'::text as method,
    r.payload ->> 'mode' as required_mode,
    jsonb_strip_nulls(jsonb_build_object(
        'sessionId', p.session_id, 'mode', r.payload -> 'mode',
        'message', r.payload -> 'message', 'toolCallId', r.payload -> 'call_id'
    )) || v.fields as params
from
    params p
    cross join responses a
    cross join completions c
    left join request r on true
    left join variants v on true;
