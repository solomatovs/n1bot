-- Новое уведомление подключённому клиенту: одна сохранённая запись -> ACP notice.
-- Вызывается обработчиком нового события; при восстановлении истории не используется.
-- Ручной просмотр: в params подставить 'sql-acp-demo', 'warning' и выполнить весь файл.
-- Результат: источник и update. После проверки адаптер отправляет update в session/update.
-- SQL не отправляет уведомление и не проверяет, доставлялось ли оно раньше.
-- Подробнее: docs/agent-history-to-acp.md, раздел «Как использовать запросы».
with
params as (
    select
        $1::text as session_id,       -- records.session_id: чат, которому показать notice
        $2::text as record_id         -- records.record_id сохранённого события, не seq
),
record as (
    select
        r.seq, r.record_id, r.payload
    from
        params p
        join history_sql.records r
            on r.session_id = p.session_id
            and r.record_id = p.record_id
    where 1=1
        and r.kind in ('notice', 'llm_request_attempt', 'task_state', 'task_notification')
        and r.copied_from_id is null
),
content as (
    select
        r.seq,
        string_agg(p.value ->> 'text', '' order by p.number) as text
    from
        record r
        cross join lateral jsonb_array_elements(r.payload -> 'content')
            with ordinality p(value, number)
    group by
        r.seq
)
select
    p.session_id,
    p.record_id,
    r.seq,
    jsonb_build_object('sessionUpdate', 'notice', 'severity', r.payload -> 'level',
        'title', c.text) as update
from
    params p
    left join record r on true
    left join content c on c.seq = r.seq;
