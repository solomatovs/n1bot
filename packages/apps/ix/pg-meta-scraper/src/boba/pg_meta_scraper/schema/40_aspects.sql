/*
pg-meta-scraper, схема, шаг 4: словарь аспектов и объявления surface_aspect для
поверхностей pg_meta_*. Тело объявления пишется dollar-quoted строкой, схема в нём
удвоена ({{schema}}), чтобы после наката в строке остался плейсхолдер: его
подставит потребитель. Накат проверяет каждое тело по контракту (node_id, content).
*/
insert into {schema}.aspect (aspect, class, description, owner) values
    ('meta_name',            'ident',           'Имя объекта как есть (relname, attname): точное совпадение и префикс.',                              'pg-meta-scraper'),
    ('meta_path',            'ident',           'Путь через точку, как пишет пользователь: schema.table или schema.table.column.',                    'pg-meta-scraper'),
    ('meta_words',           'words',           'Слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е.',              'pg-meta-scraper'),
    ('meta_description',     'description',     'Описание объекта из всего, что о нём известно скраперу: заголовок, комментарий, колонки с типами.', 'pg-meta-scraper'),
    ('meta_comment',         'description',     'Комментарий из источника как есть (obj_description, col_description).',                              'pg-meta-scraper'),
    ('meta_columns',         'description',     'Имена колонок таблицы или view через пробел: отношение находится по своим колонкам.',               'pg-meta-scraper'),
    ('meta_describer_input', 'describer_input', 'Структура таблицы или view для описателя: колонки, ключи, индексы, читаемые таблицы, оценка строк.', 'pg-meta-scraper')
on conflict (aspect) do nothing;

insert into {schema}.surface_aspect (surface, aspect, body) values
    ('pg_meta_database', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_database x
    $body$),
    ('pg_meta_database', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_database x
    $body$),
    ('pg_meta_database', 'meta_description', $body$
    select
        x.node_id,
        'Database ' || x.name
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_database x
    $body$),
    ('pg_meta_database', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_database x
    $body$),
    ('pg_meta_schema', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_schema x
    $body$),
    ('pg_meta_schema', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_schema x
    $body$),
    ('pg_meta_schema', 'meta_description', $body$
    select
        x.node_id,
        'Schema ' || x.name
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_schema x
    $body$),
    ('pg_meta_schema', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_schema x
    $body$),
    ('pg_meta_table', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_table x
    $body$),
    ('pg_meta_table', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.pg_meta_table x
    $body$),
    ('pg_meta_table', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_table x
    $body$),
    ('pg_meta_table', 'meta_description', $body$
    select
        x.node_id,
        case x.kind
            when 'partition'   then 'Partition '
            when 'partitioned' then 'Partitioned table '
            when 'foreign'     then 'Foreign table '
            else                    'Table '
        end || x.schema_name || '.' || x.name
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.pg_meta_table x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.pg_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('pg_meta_table', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_table x
    $body$),
    ('pg_meta_table', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.pg_meta_table x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.pg_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('pg_meta_table', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || case when c.not_null then ' not null' else '' end
                    || coalesce(' default ' || c.default_expr, '')
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.pg_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    fk_out as (
        select
            tr.parent_id as rel_id,
            string_agg('  ' || k.definition, E'\n' order by k.name) as text
        from
            {{schema}}.pg_meta_constraint k
            join {{schema}}.tree tr on tr.node_id = k.node_id
        where
            k.kind = 'foreign key'
        group by
            tr.parent_id
    ),
    fk_in as (
        select
            ct.parent_id as rel_id,
            string_agg(
                distinct '  ' || k.schema_name || '.' || k.table_name
                    || ': ' || k.definition,
                E'\n'
            ) as text
        from
            {{schema}}.pg_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.pg_meta_constraint k
                on  k.node_id = e.node_src_id
                and k.kind = 'foreign key'
            join {{schema}}.tree ct on ct.node_id = e.node_tgt_id
        where
            m.role = 'constraint' and m.side = 1
        group by
            ct.parent_id
    ),
    idx as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || i.name || ' (' || array_to_string(i.columns, ', ') || ')'
                    || case when i.is_unique then ' unique' else '' end
                    || coalesce(' where ' || i.predicate, ''),
                E'\n' order by i.name
            ) as text
        from
            {{schema}}.pg_meta_index i
            join {{schema}}.tree tr on tr.node_id = i.node_id
        group by
            tr.parent_id
    ),
    reads as (
        select
            e.node_src_id as rel_id,
            string_agg(
                distinct '  ' || (p.address->>'schema') || '.'
                    || coalesce(p.address->>'table', p.address->>'view'),
                E'\n'
            ) as text
        from
            {{schema}}.edge e
            join {{schema}}.node v
                on  v.id = e.node_src_id
                and v.surface = 'pg_meta_view'
            join {{schema}}.node c on c.id = e.node_tgt_id
            join {{schema}}.tree ct on ct.node_id = c.id
            join {{schema}}.node p on p.id = ct.parent_id
        group by
            e.node_src_id
    )
    select
        x.node_id,
        initcap(x.kind) || ' ' || x.schema_name || '.' || x.name
            || coalesce(E'\nComment: ' || x.comment, '')
            || case
                when x.row_estimate >= 0
                then E'\nRows (estimate): ' || x.row_estimate::bigint
                else ''
            end
            || coalesce(E'\nPartition: ' || x.partition_bound, '')
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nForeign keys:\n' || fk_out.text, '')
            || coalesce(E'\nReferenced by:\n' || fk_in.text, '')
            || coalesce(E'\nIndexes:\n' || idx.text, '')
            || coalesce(E'\nReads:\n' || reads.text, '') as content
    from
        {{schema}}.pg_meta_table x
        left join cols   on cols.rel_id = x.node_id
        left join fk_out on fk_out.rel_id = x.node_id
        left join fk_in  on fk_in.rel_id = x.node_id
        left join idx    on idx.rel_id = x.node_id
        left join reads  on reads.rel_id = x.node_id
    $body$),
    ('pg_meta_column', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_column x
    $body$),
    ('pg_meta_column', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.relation_name || '.' || x.name as content
    from
        {{schema}}.pg_meta_column x
    $body$),
    ('pg_meta_column', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_column x
    $body$),
    ('pg_meta_column', 'meta_description', $body$
    select
        x.node_id,
        'Column ' || x.schema_name || '.' || x.relation_name || '.' || x.name
            || ' ' || x.data_type
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_column x
    $body$),
    ('pg_meta_column', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_column x
    $body$),
    ('pg_meta_view', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_view x
    $body$),
    ('pg_meta_view', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.pg_meta_view x
    $body$),
    ('pg_meta_view', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_view x
    $body$),
    ('pg_meta_view', 'meta_description', $body$
    select
        x.node_id,
        case x.kind
            when 'matview' then 'Materialized view '
            else                'View '
        end || x.schema_name || '.' || x.name
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.pg_meta_view x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.pg_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('pg_meta_view', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_view x
    $body$),
    ('pg_meta_view', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.pg_meta_view x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.pg_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('pg_meta_view', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || case when c.not_null then ' not null' else '' end
                    || coalesce(' default ' || c.default_expr, '')
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.pg_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    fk_out as (
        select
            tr.parent_id as rel_id,
            string_agg('  ' || k.definition, E'\n' order by k.name) as text
        from
            {{schema}}.pg_meta_constraint k
            join {{schema}}.tree tr on tr.node_id = k.node_id
        where
            k.kind = 'foreign key'
        group by
            tr.parent_id
    ),
    fk_in as (
        select
            ct.parent_id as rel_id,
            string_agg(
                distinct '  ' || k.schema_name || '.' || k.table_name
                    || ': ' || k.definition,
                E'\n'
            ) as text
        from
            {{schema}}.pg_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.pg_meta_constraint k
                on  k.node_id = e.node_src_id
                and k.kind = 'foreign key'
            join {{schema}}.tree ct on ct.node_id = e.node_tgt_id
        where
            m.role = 'constraint' and m.side = 1
        group by
            ct.parent_id
    ),
    idx as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || i.name || ' (' || array_to_string(i.columns, ', ') || ')'
                    || case when i.is_unique then ' unique' else '' end
                    || coalesce(' where ' || i.predicate, ''),
                E'\n' order by i.name
            ) as text
        from
            {{schema}}.pg_meta_index i
            join {{schema}}.tree tr on tr.node_id = i.node_id
        group by
            tr.parent_id
    ),
    reads as (
        select
            e.node_src_id as rel_id,
            string_agg(
                distinct '  ' || (p.address->>'schema') || '.'
                    || coalesce(p.address->>'table', p.address->>'view'),
                E'\n'
            ) as text
        from
            {{schema}}.edge e
            join {{schema}}.node v
                on  v.id = e.node_src_id
                and v.surface = 'pg_meta_view'
            join {{schema}}.node c on c.id = e.node_tgt_id
            join {{schema}}.tree ct on ct.node_id = c.id
            join {{schema}}.node p on p.id = ct.parent_id
        group by
            e.node_src_id
    )
    select
        x.node_id,
        initcap(x.kind) || ' ' || x.schema_name || '.' || x.name
            || coalesce(E'\nComment: ' || x.comment, '')
            || case
                when null::float8 >= 0
                then E'\nRows (estimate): ' || null::float8::bigint
                else ''
            end
            || coalesce(E'\nPartition: ' || null::varchar, '')
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nForeign keys:\n' || fk_out.text, '')
            || coalesce(E'\nReferenced by:\n' || fk_in.text, '')
            || coalesce(E'\nIndexes:\n' || idx.text, '')
            || coalesce(E'\nReads:\n' || reads.text, '') as content
    from
        {{schema}}.pg_meta_view x
        left join cols   on cols.rel_id = x.node_id
        left join fk_out on fk_out.rel_id = x.node_id
        left join fk_in  on fk_in.rel_id = x.node_id
        left join idx    on idx.rel_id = x.node_id
        left join reads  on reads.rel_id = x.node_id
    $body$),
    ('pg_meta_index', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_index x
    $body$),
    ('pg_meta_index', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.pg_meta_index x
    $body$),
    ('pg_meta_index', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_index x
    $body$),
    ('pg_meta_index', 'meta_description', $body$
    select
        x.node_id,
        'Index ' || x.name || ' on ' || x.table_name
            || ' (' || array_to_string(x.columns, ', ') || ')'
            || case when x.is_unique then ' unique' else '' end
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_index x
    $body$),
    ('pg_meta_index', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_index x
    $body$),
    ('pg_meta_sequence', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_sequence x
    $body$),
    ('pg_meta_sequence', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.pg_meta_sequence x
    $body$),
    ('pg_meta_sequence', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_sequence x
    $body$),
    ('pg_meta_sequence', 'meta_description', $body$
    select
        x.node_id,
        'Sequence ' || x.schema_name || '.' || x.name
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_sequence x
    $body$),
    ('pg_meta_sequence', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_sequence x
    $body$),
    ('pg_meta_routine', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_routine x
    $body$),
    ('pg_meta_routine', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name
            || '(' || coalesce(x.identity_args, '') || ')' as content
    from
        {{schema}}.pg_meta_routine x
    $body$),
    ('pg_meta_routine', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_routine x
    $body$),
    ('pg_meta_routine', 'meta_description', $body$
    select
        x.node_id,
        initcap(x.kind) || ' ' || x.name
            || '(' || coalesce(x.identity_args, '') || ')'
            || coalesce(' returns ' || x.result_type, '')
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_routine x
    $body$),
    ('pg_meta_routine', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_routine x
    $body$),
    ('pg_meta_constraint', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_constraint x
    $body$),
    ('pg_meta_constraint', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_constraint x
    $body$),
    ('pg_meta_constraint', 'meta_description', $body$
    select
        x.node_id,
        initcap(x.kind) || ' ' || x.name
            || ' on ' || coalesce(x.table_name, '') || ': ' || x.definition
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_constraint x
    $body$),
    ('pg_meta_constraint', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_constraint x
    $body$),
    ('pg_meta_trigger', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_trigger x
    $body$),
    ('pg_meta_trigger', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_trigger x
    $body$),
    ('pg_meta_trigger', 'meta_description', $body$
    select
        x.node_id,
        'Trigger ' || x.name || ' on ' || x.table_name
            || ' ' || x.timing || ' ' || array_to_string(x.events, ', ')
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_trigger x
    $body$),
    ('pg_meta_trigger', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_trigger x
    $body$),
    ('pg_meta_type', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_type x
    $body$),
    ('pg_meta_type', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_type x
    $body$),
    ('pg_meta_type', 'meta_description', $body$
    select
        x.node_id,
        initcap(x.kind) || ' ' || x.schema_name || '.' || x.name
            || coalesce(' ' || x.base_type, '')
            || coalesce(' (' || array_to_string(x.enum_labels, ', ') || ')', '')
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_type x
    $body$),
    ('pg_meta_type', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_type x
    $body$),
    ('pg_meta_statistics', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.pg_meta_statistics x
    $body$),
    ('pg_meta_statistics', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.pg_meta_statistics x
    $body$),
    ('pg_meta_statistics', 'meta_description', $body$
    select
        x.node_id,
        'Statistics ' || x.name || ' on ' || x.table_name
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.pg_meta_statistics x
    $body$),
    ('pg_meta_statistics', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.pg_meta_statistics x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;
