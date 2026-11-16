-- Ответ на session/prompt ACP v1 после завершения обработки сохранённого ввода.
with
params as (
    select
        $1::text as session_id,
        $2::text as record_id
),
submitted as (
    select
        a.seq
    from
        params p
        join boba.records a
            on a.session_id = p.session_id
    where 1=1
        and a.kind = 'user_message'
        and a.record_id = p.record_id
),
finished as (
    select
        r.seq,
        r.payload
    from
        params p
        join boba.records r
            on r.session_id = p.session_id
    where 1=1
        and r.kind = 'turn_end'
        and r.seq > (select seq from submitted)
    order by
        r.seq
    limit
        1
),
completion as (
    select
        r.payload ->> 'completion' as value
    from
        params p
        join boba.records r
            on r.session_id = p.session_id
    where 1=1
        and r.kind = 'assistant_message'
        and r.seq > (select seq from submitted)
        and r.seq < (select seq from finished)
    order by
        r.seq desc
    limit
        1
)
select
    f.payload ->> 'error' as error,
    jsonb_build_object('stopReason', case
        when f.payload ->> 'outcome' = 'cancelled' then 'cancelled'
        when f.payload ->> 'outcome' = 'limit' then 'max_turn_requests'
        when (select value from completion) = 'output_limit' then 'max_tokens'
        else 'end_turn'
    end) as response
from
    finished f;
