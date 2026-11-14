-- candidate request and source metadata; validate with openai_validator.py.
-- request_id resolves to a seq; through_seq supplies an inclusive seq directly.
-- both null select the latest boundary; both supplied must identify the same seq.
with
params as (
    select
        $1::text as session_id,
        $2::text as request_id,
        $3::bigint as through_seq
),
history as not materialized (
    select
        r.seq,
        r.record_id,
        r.kind,
        r.category,
        r.role,
        r.key,
        r.operation,
        r.request_id,
        r.payload
    from
        params p
        join history_sql.records r on r.session_id = p.session_id
),
categories(category, reduction, scoped, section, strict_remove) as (
    values
        ('message',         'sum',  false,  1,      false),
        ('admission',       'sum',  false,  1,      false),
        ('request_message', 'sum',  true,   1,      false),
        ('request_context', 'last', true,   null,   false),
        ('compact',         'sum',  false,  1,      false),
        ('prompt',          'last', true,   0,      true),
        ('context',         'last', true,   1,      false),
        ('setting',         'last', true,   null,   false),
        ('tool_catalog',    'last', false,  null,   true),
        ('tool_schema',     'last', false,  null,   false)
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
        not request_local as windowed
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
    where 1=1
        and s.category <> 'admission'
    union all
    select
        a.seq,
        a.record_id,
        'message',
        null,
        a.section,
        r.role,
        r.payload
    from
        kept a
        join history h on h.seq = a.seq
        join history r on r.record_id = h.payload ->> 'source_record_id'
    where 1=1
        and a.category = 'admission'
        and r.category = 'input'
        and r.seq < a.seq
),
-- each catalog add is valid until the next actual remove; repeated adds do not close it.
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
message_records as materialized (
    select
        seq,
        record_id,
        section,
        role,
        payload ->> 'call_id'   as call_id
    from
        selected_records
    where 1=1
        and section is not null
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
        selected_records s
        cross join lateral jsonb_array_elements(s.payload -> 'content')
            with ordinality p(value, number)
    where 1=1
        and s.section is not null
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
calls as materialized (
    select
        seq,
        position,
        payload ->> 'call_id'   as call_id,
        payload ->> 'name'      as name,
        payload -> 'arguments'  as arguments
    from
        parts
    where 1=1
        and kind = 'tool_call'
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
        'tool_call_id',
        to_jsonb(call_id),
        1
    from
        message_records
    where 1=1
        and role = 'tool'
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
        coalesce(jsonb_agg(value order by section, seq), '[]')
    from
        messages
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
        kind,
        role,
        payload ->> 'call_id' as call_id,
        jsonb_typeof(payload -> 'content') as content_type
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
),
admission_sources as (
    select
        a.seq,
        a.record_id,
        h.payload ->> 'source_record_id' as source_record_id,
        r.seq as source_seq,
        r.category as source_category
    from
        inputs a
        join history h on h.seq = a.seq
        left join history r on r.record_id = h.payload ->> 'source_record_id'
    where 1=1
        and a.category = 'admission'
)
select
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
        'request_context', (select to_jsonb(c) from request_context c),
        'admission_sources', (
            select
                coalesce(jsonb_agg(to_jsonb(a) order by seq), '[]'::jsonb)
            from
                admission_sources a
        ),
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
                coalesce(jsonb_agg(to_jsonb(m) order by section, seq), '[]'::jsonb)
            from
                message_records m
        ),
        'parameter_sources', (
            select
                coalesce(jsonb_agg(to_jsonb(p) order by seq nulls first, name), '[]'::jsonb)
            from
                parameter_sources p
        )
    ) as validation_data;
