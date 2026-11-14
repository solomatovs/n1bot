-- A saved permission question -> native ACP session/request_permission.
-- Parameters: session_id, record_id of permission_request. Never called by replay.
with
params as (
    select
        $1::text as session_id,
        $2::text as record_id
),
history as not materialized (
    select
        r.session_id,
        r.seq,
        r.record_id,
        r.kind,
        r.category,
        r.role,
        r.key,
        r.operation,
        r.request_id,
        r.copied_from_id,
        r.created_at,
        r.version,
        r.payload
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
        and h.kind = 'permission_request'
        and h.copied_from_id is null
),
calls as (
    select
        part.value as payload
    from
        request q
        join history h on h.record_id = q.payload ->> 'related_message_id'
        cross join lateral jsonb_array_elements(h.payload -> 'content') part(value)
    where 1=1
        and h.role = 'assistant'
        and h.seq < q.seq
        and part.value ->> 'kind' = 'tool_call'
        and part.value ->> 'call_id' = q.payload ->> 'call_id'
),
responses as (
    select
        count(1) as count
    from
        history h
        cross join params p
    where 1=1
        and h.kind = 'permission_response'
        and h.payload ->> 'permission_request_id' = p.record_id
),
results as (
    select
        count(1) as count
    from
        history h
        cross join request q
    where 1=1
        and h.kind = 'tool_result'
        and h.payload ->> 'call_id' = q.payload ->> 'call_id'
),
kinds(decision, scope, kind) as (
    values
        ('allow', 'call', 'allow_once'),
        ('allow', 'session', 'allow_always'),
        ('deny', 'call', 'reject_once'),
        ('deny', 'session', 'reject_always')
),
options as (
    select
        jsonb_agg(jsonb_build_object(
            'optionId', o.value -> 'option_id',
            'name', o.value -> 'name',
            'kind', k.kind
        ) order by o.number) as value
    from
        request q
        cross join lateral jsonb_array_elements(q.payload -> 'options')
            with ordinality o(value, number)
        left join kinds k
            on k.decision = o.value ->> 'decision'
            and k.scope = o.value ->> 'scope'
),
subject as (
    select
        count(1) as call_count,
        jsonb_agg(jsonb_build_object(
            'type', 'tool_call',
            'toolCall', jsonb_build_object(
                'toolCallId', payload -> 'call_id',
                'name', payload -> 'name',
                'status', 'pending',
                'rawInput', payload -> 'arguments'
            )
        )) -> 0 as value
    from
        calls
)
select
    p.session_id,
    p.record_id,
    q.seq,
    a.count as response_count,
    r.count as result_count,
    s.call_count,
    'session/request_permission'::text as method,
    jsonb_strip_nulls(jsonb_build_object(
        'sessionId', p.session_id,
        'title', q.payload -> 'title',
        'description', q.payload -> 'description',
        'subject', s.value,
        'options', o.value
    )) as params
from
    params p
    cross join responses a
    cross join results r
    cross join options o
    cross join subject s
    left join request q on true;
