-- Восстановление переписки: сохранённые записи -> массив ACP session/update.
-- Ручной просмотр: в params подставить 'sql-acp-demo', null, null и выполнить весь файл.
-- Для seq=16..25: 'sql-acp-demo', 15, 25. Границы относятся к records.seq.
-- Результат: bounds, notifications, validation_data. После проверки отправляется
-- notification каждого элемента массива; SQL сам ничего не отправляет.
-- Подробнее: docs/agent-history-to-acp.md, раздел «Как использовать запросы».
with
params as (
    select
        $1::text as session_id,       -- records.session_id: какой чат читать
        $2::bigint as after_seq,      -- читать строго после этого seq; null = с начала
        $3::bigint as through_seq     -- читать по этот seq включительно; null = до конца
),
history as not materialized (
    select
        r.*
    from
        history_sql.records r
    where 1=1
        and r.session_id = (select session_id from params)
),
bounds as (
    select
        p.session_id,
        coalesce(p.after_seq, 0) as after_seq,
        coalesce(p.through_seq, h.seq, 0) as through_seq,
        h.seq as last_seq
    from
        params p
        left join lateral (
            select
                seq
            from
                history
            order by
                seq desc
            limit
                1
        ) h on true
),
-- The ACP projection owns presentation policy; history has no UI flags.
routes(kind, route) as (
    values
        ('user_message',                'user'),
        ('resource_message',            'user'),
        ('assistant_message',           'assistant'),
        ('tool_result',                 'result'),
        ('compact_message',             'compact'),
        ('slash_command',               'command'),
        ('slash_command_result',        'command_result'),
        ('notice',                      'notice'),
        ('llm_request_attempt',         'notice'),
        ('task_state',                  'notice'),
        ('turn_end',                    'diagnostic'),
        ('elicitation_request',         'diagnostic'),
        ('elicitation_response',        'diagnostic'),
        ('elicitation_completed',       'diagnostic'),
        ('permission_state',            'state'),
        ('permission_decision',         'diagnostic'),
        ('token_usage',                 'state'),
        ('subagent_session',            'state'),
        ('skill_activated',             'state'),
        ('task_notification',           'notice')
),
category_routes(category, route) as (
    values
        ('prompt',          'state'),
        ('context',         'state'),
        ('setting',         'state'),
        ('tool_catalog',    'state'),
        ('tool_schema',     'state'),
        ('request',         'state')
),
-- The index supplies message order; scalar bounds preserve that order in the plan.
stream as not materialized (
    select
        h.seq, h.record_id, h.kind, h.payload,
        coalesce(
            (select route from routes where kind = h.kind),
            (select route from category_routes where category = h.category)
        ) as route
    from
        history h
    where 1=1
        and h.seq > (select after_seq from bounds)
        and h.seq <= (select through_seq from bounds)
        and h.copied_from_id is null
    order by
        h.seq
),
-- Render one saved record at a time; CTE payloads stay local to that record.
entries as not materialized (
    select
        r.seq, e.position, e.family, e.value
    from
        stream r
        cross join lateral (
            with
            records as materialized (
                select r.*
            ),
            top_parts as materialized (
                select
                    r.seq,
                    r.record_id,
                    r.route,
                    array[p.number::integer] as position,
                    p.value ->> 'kind' as kind,
                    p.value as payload,
                    null::jsonb as resource
                from
                    records r
                    cross join lateral jsonb_array_elements(r.payload -> 'content')
                        with ordinality p(value, number)
                where 1=1
                    and r.route in ('user', 'assistant', 'result', 'compact', 'command_result', 'notice')
            ),
            parts as materialized (
                select
                    seq, record_id, route, position, kind, payload, resource
                from
                    top_parts
                union all
                select
                    p.seq,
                    p.record_id,
                    p.route,
                    p.position || c.number::integer,
                    c.value ->> 'kind', c.value, p.payload
                from
                    top_parts p
                    cross join lateral jsonb_array_elements(
                        nullif(p.payload -> 'content', 'null'::jsonb)
                    ) with ordinality c(value, number)
                where 1=1
                    and p.kind = 'resource'
            ),
            blocks as (
                select
                    seq,
                    position,
                    route,
                    'message' as channel,
                    jsonb_build_object(
                        'type', 'text',
                        'text', payload -> 'text'
                    ) as value
                from
                    parts
                where 1=1
                    and kind = 'text'
                    and resource is null
                union all
                select
                    seq, 
                    position,
                    route,
                    'thought',
                    jsonb_build_object(
                        'type', 'text',
                        'text', payload -> 'text'
                    )
                from
                    parts
                where 1=1
                    and kind = 'reasoning'
                union all
                select
                    seq,
                    position,
                    route,
                    'message',
                    jsonb_build_object(
                        'type', 'image',
                        'mimeType', payload -> 'media_type',
                        'data', translate(payload ->> 'data', '-_', '+/')
                    )
                from
                    parts
                where 1=1
                    and kind = 'image'
                    and resource is null
                union all
                select
                    seq,
                    position,
                    route,
                    'message',
                    jsonb_build_object('type', 'resource', 'resource',
                        jsonb_strip_nulls(jsonb_build_object(
                            'uri',      resource -> 'uri',
                            'mimeType', resource -> 'media_type',
                            'text',     payload  -> 'text'
                        ))
                    )
                from
                    parts
                where 1=1
                    and kind = 'text'
                    and resource is not null
                union all
                select
                    seq,
                    position,
                    route,
                    'message',
                    jsonb_build_object('type', 'resource', 'resource',
                        jsonb_build_object(
                            'uri',      resource -> 'uri',
                            'mimeType', resource -> 'media_type',
                            'blob',     translate(payload ->> 'data', '-_', '+/')
                        )
                    )
                from
                    parts
                where 1=1
                    and kind = 'image'
                    and resource is not null
                union all
                select
                    seq,
                    position,
                    route,
                    'message',
                    jsonb_strip_nulls(jsonb_build_object(
                        'type', 'resource_link',
                        'name', payload -> 'name',
                        'uri', payload -> 'uri',
                        'mimeType', payload -> 'media_type'
                    ))
                from
                    parts
                where 1=1
                    and kind = 'resource'
                    and payload -> 'content' = 'null'::jsonb
                union all
                select
                    seq, position, route, 'message',
                    jsonb_build_object('type', 'resource', 'resource',
                        jsonb_strip_nulls(jsonb_build_object('uri', payload -> 'uri',
                            'mimeType', payload -> 'media_type', 'text', '')))
                from
                    parts
                where 1=1
                    and kind = 'resource'
                    and payload -> 'content' = '[]'::jsonb
                union all
                select
                    seq, position, route, 'message',
                    jsonb_build_object('type', 'text', 'text', payload -> 'name')
                from
                    parts
                where 1=1
                    and kind = 'tool_reference'
                union all
                select
                    seq, position, route, 'call',
                    jsonb_build_object('sessionUpdate', 'tool_call_update',
                        'toolCallId', payload -> 'call_id', 'name', payload -> 'name',
                        'title', payload -> 'name', 'kind', 'other', 'status', 'pending',
                        'rawInput', payload -> 'arguments')
                from
                    parts
                where 1=1
                    and kind = 'tool_call'
            ),
            -- Separate adjacent message/thought/call runs without moving text across a tool call.
            run_starts as (
                select
                    b.*,
                    (channel = 'call' or channel is distinct from lag(channel) over (
                        partition by seq order by position
                    ))::integer as starts_run
                from
                    blocks b
            ),
            run_parts as (
                select
                    b.*,
                    sum(starts_run) over (partition by seq order by position) as run
                from
                    run_starts b
            ),
            content as materialized (
                select
                    seq, run, channel, min(position) as position,
                    jsonb_agg(value order by position) as value
                from
                    run_parts
                group by
                    seq, run, channel
            ),
            -- Join once: a filtered join in each update branch can repeatedly scan content.
            message_content as materialized (
                select
                    r.seq, r.record_id, r.route, c.position, c.channel, c.value
                from
                    records r
                    join content c on c.seq = r.seq
            ),
            tool_results as materialized (
                select
                    r.seq, r.record_id,
                    jsonb_build_object('sessionUpdate', 'tool_call_update',
                        'toolCallId', r.payload -> 'call_id', 'status', s.status,
                        'content', jsonb_agg(jsonb_build_object('type', 'content', 'content', b.value) order by b.position)
                    ) as value
                from
                    blocks b
                    join records r on r.seq = b.seq
                    join (values (false, 'completed'), (true, 'failed')) s(is_error, status)
                        on s.is_error = (r.payload ->> 'is_error')::boolean
                where 1=1
                    and b.route = 'result'
                group by
                    r.seq, r.record_id, r.payload -> 'call_id', s.status
            ),
            notice_text as materialized (
                select
                    r.seq, r.record_id, r.kind, r.payload,
                    string_agg(p.value ->> 'text', '' order by p.number) as text
                from
                    records r
                    cross join lateral jsonb_array_elements(r.payload -> 'content')
                        with ordinality p(value, number)
                where 1=1
                    and r.route = 'notice'
                group by
                    r.seq, r.record_id, r.kind, r.payload
            ),
            updates as (
                select
                    r.seq, r.record_id, r.position,
                    jsonb_build_object('sessionUpdate', 'user_message',
                        'messageId', r.record_id, 'content', r.value) as value
                from
                    message_content r
                where 1=1
                    and r.route = 'user'
                union all
                select
                    r.seq, r.record_id, r.position,
                    jsonb_build_object('sessionUpdate', 'agent_' || r.channel,
                        'messageId', jsonb_build_array(r.record_id, r.position)::text,
                        'content', r.value)
                from
                    message_content r
                where 1=1
                    and r.route = 'assistant'
                    and r.channel in ('message', 'thought')
                union all
                select
                    r.seq, r.record_id, r.position,
                    r.value -> 0
                from
                    message_content r
                where 1=1
                    and r.route = 'assistant'
                    and r.channel = 'call'
                union all
                select
                    seq, record_id, array[0], value
                from
                    tool_results
                union all
                select
                    seq, record_id, array[0],
                    jsonb_build_object('sessionUpdate', 'tool_call_update',
                        'toolCallId', payload -> 'call_id', 'status', s.status, 'content', '[]'::jsonb)
                from
                    records
                    join (values (false, 'completed'), (true, 'failed')) s(is_error, status)
                        on s.is_error = (payload ->> 'is_error')::boolean
                where 1=1
                    and route = 'result'
                    and payload -> 'content' = '[]'::jsonb
                union all
                select
                    r.seq, r.record_id, array[0],
                    jsonb_build_object('sessionUpdate', 'compaction_update', 'compactionId', r.record_id,
                        'status', 'completed', 'summary', r.value)
                from
                    message_content r
                where 1=1
                    and r.route = 'compact'
                union all
                select
                    seq, record_id, array[0],
                    jsonb_build_object('sessionUpdate', 'user_message', 'messageId', record_id,
                        'content', jsonb_build_array(jsonb_build_object('type', 'text', 'text', payload -> 'text')))
                from
                    records
                where 1=1
                    and route = 'command'
                union all
                select
                    r.seq, r.record_id, array[0],
                    jsonb_build_object('sessionUpdate', 'agent_message',
                        'messageId', jsonb_build_array(r.record_id, array[0])::text,
                        'content', coalesce(c.value, '[]'::jsonb))
                from
                    records r
                    left join content c on c.seq = r.seq
                where 1=1
                    and r.route = 'command_result'
                union all
                select
                    r.seq, r.record_id, array[1],
                    jsonb_build_object('sessionUpdate', 'agent_message',
                        'messageId', jsonb_build_array(r.record_id, array[1])::text,
                        'content', jsonb_build_array(jsonb_build_object('type', 'text',
                            'text', '[' || (r.payload ->> 'level') || '] ' || coalesce(r.text, r.kind)))
                            || jsonb_build_array(jsonb_build_object('type', 'resource', 'resource',
                                jsonb_build_object('uri', 'urn:boba:history:' || encode(convert_to(r.record_id, 'utf8'), 'hex'),
                                    'mimeType', 'application/json', 'text', (r.payload - 'content')::text))))
                from
                    notice_text r
                union all
                select
                    seq, record_id, array[0],
                    jsonb_build_object('sessionUpdate', 'agent_message',
                        'messageId', jsonb_build_array(record_id, array[0])::text,
                        'content', jsonb_build_array(
                            jsonb_build_object('type', 'text', 'text', kind || ': ' ||
                                coalesce(payload ->> 'message', payload ->> 'action', payload ->> 'outcome', '')),
                            jsonb_build_object('type', 'resource', 'resource',
                                jsonb_build_object('uri', 'urn:boba:history:' || encode(convert_to(record_id, 'utf8'), 'hex'),
                                    'mimeType', 'application/json', 'text', payload::text))))
                from
                    records
                where 1=1
                    and route = 'diagnostic'
                union all
                select
                    seq, record_id, array[0],
                    jsonb_build_object('sessionUpdate', 'subagent_update',
                        'sessionId', payload #> '{state,child_session_id}')
                from
                    records
                where 1=1
                    and kind = 'task_state'
                    and payload #>> '{state,child_session_id}' is not null
            
            )
            select
                u.position, 'notification'::text as family,
                jsonb_build_object(
                    'seq', u.seq, 'record_id', u.record_id, 'position', u.position,
                    'notification', jsonb_build_object('jsonrpc', '2.0', 'method', 'session/update',
                        'params', jsonb_build_object('sessionId', (select session_id from params), 'update', u.value))
                ) as value
            from
                updates u
            union all
            select
                position, 'part', jsonb_build_object('seq', seq, 'record_id', record_id,
                    'position', position, 'kind', kind, 'route', route)
            from
                parts
            union all
            select
                array[0], 'unmapped', jsonb_build_object('seq', seq, 'record_id', record_id, 'kind', kind)
            from
                records
            where 1=1
                and route is null
        ) e
    where 1=1
        and r.route is distinct from 'state'
    order by
        r.seq, e.position
),
folded as (
    select
        coalesce(jsonb_agg(e.value order by e.seq, e.position)
            filter (where e.family = 'notification'), '[]'::jsonb) as notifications,
        coalesce(jsonb_agg(e.value order by e.seq, e.position)
            filter (where e.family = 'part'), '[]'::jsonb) as parts,
        coalesce(jsonb_agg(e.value order by e.seq)
            filter (where e.family = 'unmapped'), '[]'::jsonb) as unmapped
    from
        entries e

)

select
    to_jsonb(b) as bounds,
    f.notifications,
    jsonb_build_object(
        'unmapped_records', f.unmapped,
        'parts', f.parts,
        'copies', (
            select
                coalesce(jsonb_agg(
                    jsonb_build_object(
                    'seq', h.seq,
                    'record_id', h.record_id,
                    'kind', h.kind,
                    'category', h.category,
                    'copied_from_id', h.copied_from_id,
                    'source_seq', s.seq,
                    'source_kind', s.kind,
                    'source_category', s.category
                ) order by h.seq), '[]'::jsonb)
            from
                history h
                left join history s on s.record_id = h.copied_from_id
            where 1=1
                and h.copied_from_id is not null
                and h.seq <= b.through_seq
        ),
        'calls', (
            select
                coalesce(jsonb_agg(jsonb_build_object('seq', h.seq, 'record_id', h.record_id,
                    'call_id', p.value -> 'call_id') order by h.seq, p.number), '[]'::jsonb)
            from
                history h
                cross join lateral jsonb_array_elements(h.payload -> 'content')
                    with ordinality p(value, number)
            where 1=1
                and h.kind = 'assistant_message'
                and h.copied_from_id is null
                and h.seq <= b.through_seq
                and p.value ->> 'kind' = 'tool_call'
        ),
        'results', (
            select
                coalesce(jsonb_agg(jsonb_build_object('seq', h.seq, 'record_id', h.record_id,
                    'call_id', h.payload -> 'call_id') order by h.seq), '[]'::jsonb)
            from
                history h
            where 1=1
                and h.kind = 'tool_result'
                and h.copied_from_id is null
                and h.seq <= b.through_seq
        )
    ) as validation_data
from
    bounds b
    cross join folded f;
