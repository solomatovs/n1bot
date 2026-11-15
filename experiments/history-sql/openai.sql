-- candidate request and source metadata; validate with openai_validator.py.
-- request_id resolves to a seq; through_seq supplies an inclusive seq directly.
-- both null select the latest boundary; both supplied must identify the same seq.
with
params as (
    select
        'sql-task-demo'::text as session_id,
        null::text as request_id,
        null::bigint as through_seq
),
history as not materialized (
    select
        r.seq,
        r.record_id,
        r.category,
        r.role,
        r.key,
        r.operation,
        r.request_id,
        r.payload
    from
        params p
        join boba.records r on r.session_id = p.session_id
),
categories(category, reduction, scoped, section, strict_remove) as (
    values
        ('message',         'sum',  false,  1,      false),
        ('request_message', 'sum',  true,   1,      false),
        ('request_context', 'last', true,   null,   false),
        ('tool_call',       'sum',  false,  null,   false),
        ('tool_status',     'last', false,  null,   false),
        ('tool_result',     'last', false,  null,   false),
        ('compact',         'sum',  false,  1,      false),
        ('prompt',          'last', true,   0,      true),
        ('context',         'last', true,   1,      false),
        ('setting',         'last', true,   null,   false),
        ('tool_catalog',    'last', false,  null,   true),
        ('tool_schema',     'last', false,  null,   false),
        ('request',        null,   false,  null,   false),
        ('operational',    null,   false,  null,   false)
),
roles(role, empty_content) as (
    values
        ('system',    '""'::jsonb),
        ('user',      '""'::jsonb),
        ('assistant', 'null'::jsonb),
        ('tool',      '""'::jsonb)
),
requested_boundary as materialized (
    select
        h.seq,
        h.record_id as request_id
    from
        history h
        cross join params p
    where 1=1
        and h.category = 'request'
        and (p.through_seq is null or p.request_id is not null)
        and (p.request_id is null or h.record_id = p.request_id)
    order by
        h.seq desc
    limit
        1
),
target as (
    select
        coalesce(p.through_seq, b.seq) as seq
    from
        params p
        left join requested_boundary b on true
),
selection as (
    select
        p.request_id,
        p.through_seq,
        b.seq as request_seq,
        coalesce((
            select
                h.seq
            from
                history h
            order by
                h.seq desc
            limit
                1
        ), 0) as last_seq
    from
        params p
        left join requested_boundary b on true
),
compact as (
    select
        coalesce(max(r.seq), 0) as seq
    from
        history r
        cross join target t
    where 1=1
        and r.category = 'compact'
        and r.seq <= t.seq
),
active_records as materialized (
    select
        r.seq,
        r.record_id,
        r.category,
        r.key,
        r.operation,
        r.request_id
    from
        history r
        cross join target t
        cross join compact c
    where 1=1
        and r.category <> 'operational'
        and r.seq >= c.seq
        and r.seq <= t.seq
),
unknown_categories as (
    select
        r.seq,
        r.record_id,
        r.category
    from
        active_records r
        left join categories c on c.category = r.category
    where 1=1
        and c.category is null
),
inputs as materialized (
    select
        h.seq,
        h.record_id,
        h.category,
        h.key,
        h.operation,
        h.request_id,
        c.reduction,
        c.scoped,
        c.section,
        c.strict_remove,
        c.scoped and h.request_id is not null as request_local
    from
        active_records h
        join categories c
            on c.category = h.category
),
request_boundaries as materialized (
    select
        seq,
        record_id as request_id
    from
        active_records
    where 1=1
        and category = 'request'
),
scoped_inputs as materialized (
    select
        i.seq,
        i.record_id,
        i.category,
        i.key,
        i.operation,
        i.request_id,
        i.reduction,
        i.scoped,
        i.section,
        i.strict_remove,
        i.request_local
    from
        inputs i
        cross join target t
        left join request_boundaries b on b.request_id = i.request_id
    where 1=1
        and (not i.request_local or b.seq is null or b.seq = t.seq)
),
changes as (
    select
        i.seq,
        i.record_id,
        i.category,
        i.key,
        i.operation,
        i.request_id,
        i.reduction,
        i.scoped,
        i.section,
        i.strict_remove,
        i.request_local,
        row_number() over (partition by i.category, i.key order by i.request_local desc, i.seq desc) as rank
    from
        scoped_inputs i
    where 1=1
        and i.reduction = 'last'
),
selected as materialized (
    select
        seq,
        record_id,
        category,
        key,
        section,
        request_local,
        not request_local and category <> 'tool_call' as windowed
    from
        scoped_inputs
    where 1=1
        and reduction = 'sum'
        and operation = 'set'
    union all
    select
        seq,
        record_id,
        category,
        key,
        section,
        request_local,
        false
    from
        changes
    where 1=1
        and rank = 1
        and operation = 'set'
),
request_context as (
    select
        s.seq,
        s.record_id,
        coalesce(h.payload ->> 'mode', 'conversation') as mode,
        (h.payload ->> 'from_seq')::bigint as from_seq,
        (h.payload ->> 'through_seq')::bigint as through_seq
    from
        params p
        left join selected s on s.category = 'request_context'
        left join history h on h.seq = s.seq
),
kept as materialized (
    select
        s.seq,
        s.record_id,
        s.category,
        s.key,
        s.section,
        s.request_local,
        s.windowed
    from
        selected s
        cross join request_context c
    where 1=1
        and (c.mode = 'conversation' or s.request_local or s.category in ('setting', 'request_context'))
        and (not s.windowed or c.from_seq is null or s.seq >= c.from_seq)
        and (not s.windowed or c.through_seq is null or s.seq <= c.through_seq)
),
selected_records as materialized (
    select
        s.seq,
        s.record_id,
        s.category,
        s.key,
        s.section,
        r.role,
        r.payload
    from
        kept s
        join history r on r.seq = s.seq
),
catalog as materialized (
    select
        seq,
        record_id,
        key,
        operation,
        min(seq) filter (where operation = 'remove') over (
            partition by key order by seq rows between current row and unbounded following
        ) as removed_at
    from
        active_records
    where 1=1
        and category = 'tool_catalog'
),
schemas as materialized (
    select
        s.seq,
        s.record_id,
        s.key,
        r.payload,
        c.seq as catalog_seq,
        c.key as catalog_key,
        c.operation, c.removed_at
    from
        active_records s
        join history r on r.seq = s.seq
        left join catalog c on c.record_id = r.payload ->> 'catalog_revision'
    where 1=1
        and s.category = 'tool_schema'
),
tools as (
    select
        s.seq,
        jsonb_build_object(
            'type', 'function',
            'function', jsonb_build_object(
                'name',         s.key,
                'description',  s.payload #> '{tool,description}',
                'parameters',   s.payload #> '{tool,parameters}'
            )
        ) as value
    from
        schemas s
        join kept a
            on a.seq = s.seq
    where 1=1
        and s.removed_at is null
),
call_definitions as materialized (
    select
        seq,
        record_id,
        key as call_id,
        payload ->> 'assistant_record_id' as assistant_id,
        payload ->> 'name' as name,
        payload -> 'arguments' as arguments,
        payload ->> 'task_id' as task_id
    from
        selected_records
    where 1=1
        and category = 'tool_call'
),
call_records as materialized (
    select
        c.seq,
        c.record_id,
        c.call_id,
        c.assistant_id,
        c.name,
        c.arguments,
        c.task_id
    from
        call_definitions c
        left join history h on h.record_id = c.assistant_id
        left join selected_records p on p.record_id = c.assistant_id
    where 1=1
        and (h.seq is null or p.seq is not null)
),
fixed_results as materialized (
    select
        s.seq,
        s.record_id,
        s.key as call_id,
        s.payload ->> 'call_record_id' as call_record_id,
        s.payload ->> 'status_record_id' as status_record_id,
        s.payload
    from
        selected_records s
        left join call_definitions c on c.call_id = s.key
        left join call_records a on a.seq = c.seq
    where 1=1
        and s.category = 'tool_result'
        and (c.seq is null or a.seq is not null)
),
status_records as materialized (
    select
        h.seq,
        h.record_id,
        h.key,
        h.payload
    from
        fixed_results r
        join history h on h.record_id = r.status_record_id
    where 1=1
        and h.category = 'tool_status'
        and h.seq < r.seq
    group by
        h.seq, h.record_id, h.key, h.payload
),
content_records as materialized (
    select
        s.seq,
        s.record_id,
        s.section,
        coalesce(s.role, 'tool') as role,
        s.payload
    from
        selected_records s
    where 1=1
        and (s.section is not null or s.category in ('tool_result', 'tool_status'))
    union all
    select
        r.seq,
        r.record_id,
        null,
        'tool',
        r.payload
    from
        status_records r
        left join selected_records s on s.seq = r.seq
    where 1=1
        and s.seq is null
),
message_records as materialized (
    select
        seq,
        record_id,
        section,
        role
    from
        content_records
),
top_parts as materialized (
    select
        s.seq,
        s.record_id,
        array[p.number::integer] as position,
        p.value ->> 'kind' as kind,
        p.value as payload,
        s.role
    from
        content_records s
        cross join lateral jsonb_array_elements(s.payload -> 'content')
            with ordinality p(value, number)
),
parts as materialized (
    select
        seq, record_id, position, kind, payload, role
    from
        top_parts
    union all
    select
        p.seq,
        p.record_id,
        p.position || c.number::integer,
        c.value ->> 'kind',
        c.value,
        p.role
    from
        top_parts p
        cross join lateral jsonb_array_elements(nullif(p.payload -> 'content', 'null'::jsonb))
            with ordinality c(value, number)
    where 1=1
        and p.kind = 'resource'
),
call_references as (
    select
        seq,
        record_id as assistant_id,
        position,
        payload ->> 'call_id' as call_id
    from
        parts
    where 1=1
        and kind = 'tool_call_reference'
),
calls as materialized (
    select
        p.seq,
        p.assistant_id,
        p.position,
        coalesce(p.call_id, r.call_id) as call_id,
        r.seq as call_seq,
        r.record_id as call_record_id,
        r.assistant_id as call_assistant_id,
        r.name,
        r.arguments,
        r.task_id
    from
        call_references p
        full join call_records r on r.call_id = p.call_id
),
part_values as (
    select
        seq,
        position,
        'content' as field,
        jsonb_build_object(
            'type', 'text',
            'text', payload -> 'text'
        ) as value
    from
        parts
    where 1=1
        and kind = 'text'
    union all
    select
        seq,
        position,
        'content',
        jsonb_build_object('type', 'text', 'text', payload -> 'name')
    from
        parts
    where 1=1
        and kind = 'tool_reference'
    union all
    select
        seq,
        position,
        'content',
        jsonb_build_object(
            'type', 'image_url',
            'image_url', jsonb_build_object(
                'url', 'data:' || (payload ->> 'media_type') || ';base64,' || translate(payload ->> 'data', '-_', '+/')
            )
        )
    from
        parts
    where 1=1
        and kind = 'image'
    union all
    select
        seq,
        position,
        'tool_calls',
        jsonb_build_object(
            'id', call_id,
            'type', 'function',
            'function', jsonb_build_object('name', name, 'arguments', arguments::text)
        )
    from
        calls
    where 1=1
        and seq is not null
),
part_arrays as (
    select
        seq,
        field,
        jsonb_agg(value order by position) as value
    from
        part_values
    group by
        seq, field
),
message_fields as (
    select
        m.seq,
        m.section,
        f.name,
        f.value,
        0 as priority
    from
        message_records m
        join roles r on r.role = m.role
        cross join lateral (values ('role', to_jsonb(m.role)), ('content', r.empty_content)) f(name, value)
    union all
    select
        seq,
        null,
        field,
        value,
        1
    from
        part_arrays
    union all
    select
        seq,
        null,
        'reasoning_content',
        to_jsonb(string_agg(payload ->> 'text', '' order by position)),
        1
    from
        parts
    where 1=1
        and kind = 'reasoning'
        and role = 'assistant'
    group by
        seq
),
messages as (
    select
        seq,
        min(section) as section,
        jsonb_object_agg(name, value order by priority) as value
    from
        message_fields
    group by
        seq
),
tool_sources as materialized (
    select
        coalesce(c.call_id, r.call_id) as call_id,
        c.seq as assistant_seq,
        c.assistant_id,
        c.call_seq,
        c.call_record_id,
        c.call_assistant_id,
        c.position,
        c.task_id,
        r.seq as result_seq,
        r.record_id as result_id,
        r.call_record_id as result_call_record_id,
        (r.payload ->> 'is_error')::boolean as result_is_error,
        r.status_record_id,
        o.seq as pinned_seq,
        o.key as pinned_task_id,
        o.payload #>> '{state,status}' as pinned_status,
        t.seq as task_seq,
        coalesce(o.seq, r.seq, t.seq) as source_seq,
        coalesce(o.record_id, r.record_id, t.record_id) as source_id
    from
        calls c
        full join fixed_results r on r.call_id = c.call_id
        left join status_records o on o.record_id = r.status_record_id
        left join selected_records t
            on t.category = 'tool_status'
            and t.key = c.task_id
),
dialogue as materialized (
    select
        m.seq,
        h.record_id,
        m.section,
        h.role,
        null::text as call_id,
        array[m.seq, 0::bigint] as ordering,
        m.value
    from
        messages m
        join history h on h.seq = m.seq
    where 1=1
        and m.section is not null
    union all
    select
        t.source_seq,
        t.source_id,
        1,
        'tool',
        t.call_id,
        array[t.assistant_seq, t.position[1]::bigint],
        m.value || jsonb_build_object('tool_call_id', t.call_id)
    from
        tool_sources t
        join messages m on m.seq = t.source_seq
    where 1=1
        and t.assistant_seq is not null
),
request_fields as materialized (
    select
        s.seq,
        p.key as name,
        p.value
    from
        selected_records s
        cross join lateral jsonb_each(s.payload -> 'value') p
    where 1=1
        and s.category = 'setting'
    union all
    select
        null,
        'messages',
        coalesce(jsonb_agg(value order by section, ordering), '[]')
    from
        dialogue
    union all
    select
        null,
        'tools',
        jsonb_agg(value order by seq)
    from
        tools
    having
        count(1) > 0
),
-- source metadata for the pure python validator.
state_sources as (
    select
        seq,
        record_id,
        category,
        key,
        operation,
        request_id,
        request_local,
        strict_remove
    from
        inputs
    where 1=1
        and (reduction = 'last' or request_local)
),
schema_sources as (
    select
        seq,
        record_id,
        key,
        payload ->> 'catalog_revision' as catalog_revision,
        catalog_seq,
        catalog_key,
        operation as catalog_operation,
        removed_at
    from
        schemas
),
content_sources as (
    select
        seq,
        record_id,
        position,
        case kind when 'tool_call_reference' then 'tool_call' else kind end as kind,
        role,
        payload ->> 'call_id' as call_id,
        jsonb_typeof(payload -> 'content') as content_type,
        payload ->> 'format' as signature_format
    from
        parts
),
parameter_sources as (
    select
        f.seq,
        h.record_id,
        f.name
    from
        request_fields f
        left join active_records h
            on h.seq = f.seq
)

select
    -- setting/model supplies the connection outside the inference body.
    (
        select
            payload ->> 'provider'
        from
            selected_records
        where 1=1
            and category = 'setting'
            and key = 'model'
    ) as provider,
    (
        select
            jsonb_object_agg(name, value order by name)
        from
            request_fields
    ) as request,
    jsonb_build_object(
        'unknown_categories', (
            select
                coalesce(jsonb_agg(to_jsonb(c) order by seq), '[]'::jsonb)
            from
                unknown_categories c
        ),
        'tool_sources', (
            select
                coalesce(jsonb_agg(to_jsonb(t) order by assistant_seq, position), '[]'::jsonb)
            from
                tool_sources t
        ),
        'request_context', (select to_jsonb(c) from request_context c),
        'selection', (
            select
                to_jsonb(s)
            from
                selection s
        ),
        'request_boundary', (
            select
                to_jsonb(b)
            from
                request_boundaries b
                cross join target t
            where 1=1
                and b.seq = t.seq
        ),
        'compact_seq', (
            select
                seq
            from
                compact
        ),
        'request_boundaries', (
            select
                coalesce(jsonb_agg(to_jsonb(r) order by seq), '[]'::jsonb)
            from
                request_boundaries r
        ),
        'state_sources', (
            select
                coalesce(jsonb_agg(to_jsonb(s) order by seq), '[]'::jsonb)
            from
                state_sources s
        ),
        'schema_sources', (
            select
                coalesce(jsonb_agg(to_jsonb(s) order by seq), '[]'::jsonb)
            from
                schema_sources s
        ),
        'content_sources', (
            select
                coalesce(jsonb_agg(to_jsonb(c) order by seq, position), '[]'::jsonb)
            from
                content_sources c
        ),
        'message_sources', (
            select
                coalesce(jsonb_agg(jsonb_build_object(
                    'seq', seq, 'record_id', record_id, 'section', section,
                    'role', role, 'call_id', call_id
                ) order by section, ordering), '[]'::jsonb)
            from
                dialogue m
        ),
        'parameter_sources', (
            select
                coalesce(jsonb_agg(to_jsonb(p) order by seq nulls first, name), '[]'::jsonb)
            from
                parameter_sources p
        )
    ) as validation_data;
