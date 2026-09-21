/*
ch-meta-scraper, схема, шаг 4: аспекты поверхностей ch_meta_*.

Скрапер кладёт объекты ClickHouse в surface-таблицы ch_meta_* (шаг 3), а поиск работает
не по строкам этих таблиц, а по текстам объекта: имени, пути, словам имени, описанию,
карточке для модели. Индексаторы и описатель про ch_meta_* ничего не знают: каждый
подписан на классы аспектов и берёт тексты из объявлений {schema}.surface_aspect.
Имена аспектов те же, что у pg-meta-scraper, словарь общий: строки уже могут быть
вписаны им, тогда insert ничего не меняет.

Каждое тело отдаёт по строке на node две колонки node_id и content; пустой content
потребитель отбрасывает. Схема в теле удвоена: после наката в строке остаётся
плейсхолдер схемы, его подставит потребитель. Накат проверяет каждое тело по контракту.
*/

insert into {schema}.aspect (aspect, class, description, owner) values
    ('meta_name',            'ident',           'Имя объекта как есть: точное совпадение и префикс.',                                                        'ch-meta-scraper'),
    ('meta_path',            'ident',           'Путь через точку, как пишет пользователь: database.table или database.table.column.',                     'ch-meta-scraper'),
    ('meta_words',           'words',           'Слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е.',                      'ch-meta-scraper'),
    ('meta_description',     'description',     'Описание объекта из всего, что о нём известно скраперу: заголовок, движок, комментарий, колонки с типами.', 'ch-meta-scraper'),
    ('meta_comment',         'description',     'Комментарий из источника как есть.',                                                                        'ch-meta-scraper'),
    ('meta_columns',         'description',     'Имена колонок таблицы, представления или словаря через пробел: отношение находится по своим колонкам.',   'ch-meta-scraper'),
    ('meta_describer_input', 'describer_input', 'Структура таблицы, представления или словаря для описателя: движок, ключи, колонки, индексы, связи.',     'ch-meta-scraper')
on conflict (aspect) do nothing;


/*
ch_meta_server — сервер, корень источника. Пример — edge-ch-25.12:
    meta_name         172.17.0.26:8123
    meta_description  ClickHouse server 172.17.0.26:8123 version 25.12.12.1
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_server', 'meta_name', $body$
    select
        x.node_id,
        x.host || ':' || x.port as content
    from
        {{schema}}.ch_meta_server x
    $body$),
    ('ch_meta_server', 'meta_words', $body$
    select
        x.node_id,
        lower(replace(x.host, '.', ' ')) as content
    from
        {{schema}}.ch_meta_server x
    $body$),
    ('ch_meta_server', 'meta_description', $body$
    select
        x.node_id,
        'ClickHouse server ' || x.host || ':' || x.port
            || coalesce(' version ' || x.version, '') as content
    from
        {{schema}}.ch_meta_server x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_database — база. Пример — edge_demo без комментария, meta_comment пуст и отброшен:
    meta_name         edge_demo
    meta_words        edge demo
    meta_description  Database edge_demo (Atomic)
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_database', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_database x
    $body$),
    ('ch_meta_database', 'meta_words', $body$
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
        {{schema}}.ch_meta_database x
    $body$),
    ('ch_meta_database', 'meta_description', $body$
    select
        x.node_id,
        'Database ' || x.name || coalesce(' (' || x.engine || ')', '')
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.ch_meta_database x
    $body$),
    ('ch_meta_database', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ch_meta_database x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_table — таблица. meta_describer_input — карточка для описателя: движок, ключи,
колонки, индексы, проекции, связи по зависимостям каталога. Пример — edge_demo.orders:
    meta_name         orders
    meta_path         edge_demo.orders
    meta_words        orders
    meta_description  Table edge_demo.orders (MergeTree): Заказы. Columns: id (UInt64),
                      customer_id (UInt64), amount (Decimal(18, 2)), ...
    meta_columns      id customer_id amount status created_at day amount_rub
    meta_describer_input
        Table edge_demo.orders
        Engine: MergeTree PARTITION BY toYYYYMM(created_at) ...
        Comment: Заказы
        Rows: 0
        Partition key: toYYYYMM(created_at)
        Sorting key: customer_id, intHash32(customer_id), created_at
        Primary key: customer_id, intHash32(customer_id)
        Sampling key: intHash32(customer_id)
        Columns:
          id UInt64
          customer_id UInt64 -- Клиент, см. customers
          ...
        Indexes:
          idx_status set(3) on status granularity 4
        Projections:
          p_by_status: SELECT status, count() GROUP BY status
        Used by:
          edge_demo.mv_daily_sales
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_table', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_table x
    $body$),
    ('ch_meta_table', 'meta_path', $body$
    select
        x.node_id,
        x.database_name || '.' || x.name as content
    from
        {{schema}}.ch_meta_table x
    $body$),
    ('ch_meta_table', 'meta_words', $body$
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
        {{schema}}.ch_meta_table x
    $body$),
    ('ch_meta_table', 'meta_description', $body$
    select
        x.node_id,
        'Table ' || x.database_name || '.' || x.name || coalesce(' (' || x.engine || ')', '')
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.ch_meta_table x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ch_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ch_meta_table', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ch_meta_table x
    $body$),
    ('ch_meta_table', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.ch_meta_table x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ch_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ch_meta_table', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || coalesce(' ' || c.default_kind || ' ' || c.default_expression, '')
                    || coalesce(' codec ' || c.codec, '')
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.ch_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    links as (
        select
            e.node_src_id as rel_id,
            m.role::text as role,
            string_agg(
                distinct '  ' || (p.address->>'database') || '.'
                    || coalesce(p.address->>'table', p.address->>'view', p.address->>'dictionary'),
                E'\n'
            ) as text
        from
            {{schema}}.ch_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node p on p.id = e.node_tgt_id
        where
            m.role in ('loading', 'target')
        group by
            e.node_src_id, m.role
    ),
    dependents as (
        select
            e.node_tgt_id as rel_id,
            string_agg(
                distinct '  ' || (s.address->>'database') || '.'
                    || coalesce(s.address->>'table', s.address->>'view', s.address->>'dictionary'),
                E'\n'
            ) as text
        from
            {{schema}}.ch_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node s on s.id = e.node_src_id
        where
            m.role = 'dependency'
        group by
            e.node_tgt_id
    ),
    idx as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || i.name || ' ' || i.kind_full || ' on ' || i.expr
                    || ' granularity ' || i.granularity,
                E'\n' order by i.name
            ) as text
        from
            {{schema}}.ch_meta_index i
            join {{schema}}.tree tr on tr.node_id = i.node_id
        group by
            tr.parent_id
    ),
    proj as (
        select
            tr.parent_id as rel_id,
            string_agg('  ' || p.name || ': ' || p.query, E'\n' order by p.name) as text
        from
            {{schema}}.ch_meta_projection p
            join {{schema}}.tree tr on tr.node_id = p.node_id
        group by
            tr.parent_id
    )
    select
        x.node_id,
        'Table ' || x.database_name || '.' || x.name
            || coalesce(E'\nEngine: ' || x.engine_full, E'\nEngine: ' || x.engine, '')
            || coalesce(E'\nComment: ' || x.comment, '')
            || coalesce(E'\nRows: ' || x.total_rows, '')
            || coalesce(E'\nPartition key: ' || x.partition_key, '')
            || coalesce(E'\nSorting key: ' || x.sorting_key, '')
            || coalesce(E'\nPrimary key: ' || x.primary_key, '')
            || coalesce(E'\nSampling key: ' || x.sampling_key, '')
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nIndexes:\n' || idx.text, '')
            || coalesce(E'\nProjections:\n' || proj.text, '')
            || coalesce(E'\nLoads from:\n' || loads.text, '')
            || coalesce(E'\nUsed by:\n' || dependents.text, '') as content
    from
        {{schema}}.ch_meta_table x
        left join cols        on cols.rel_id = x.node_id
        left join idx         on idx.rel_id = x.node_id
        left join proj        on proj.rel_id = x.node_id
        left join links loads on loads.rel_id = x.node_id and loads.role = 'loading'
        left join dependents  on dependents.rel_id = x.node_id
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_view — представление. Пример — edge_demo.mv_daily_sales:
    meta_name         mv_daily_sales
    meta_path         edge_demo.mv_daily_sales
    meta_words        mv daily sales
    meta_description  Materialized view edge_demo.mv_daily_sales. Columns: day (Date), total (Decimal(38, 2))
                      (у обычного представления заголовок View edge_demo.v_paid)
    meta_describer_input
        Materialized view edge_demo.mv_daily_sales
        Query: SELECT toDate(created_at) AS day, sum(amount) AS total FROM edge_demo.orders GROUP BY day
        Columns:
          day Date
          total Decimal(18, 2)
        Loads from:
          edge_demo.daily_sales
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_view', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_view x
    $body$),
    ('ch_meta_view', 'meta_path', $body$
    select
        x.node_id,
        x.database_name || '.' || x.name as content
    from
        {{schema}}.ch_meta_view x
    $body$),
    ('ch_meta_view', 'meta_words', $body$
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
        {{schema}}.ch_meta_view x
    $body$),
    ('ch_meta_view', 'meta_description', $body$
    select
        x.node_id,
        case x.kind when 'view' then 'View ' else initcap(x.kind) || ' view ' end
            || x.database_name || '.' || x.name
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.ch_meta_view x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ch_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ch_meta_view', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ch_meta_view x
    $body$),
    ('ch_meta_view', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.ch_meta_view x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ch_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ch_meta_view', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || coalesce(' ' || c.default_kind || ' ' || c.default_expression, '')
                    || coalesce(' codec ' || c.codec, '')
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.ch_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    links as (
        select
            e.node_src_id as rel_id,
            m.role::text as role,
            string_agg(
                distinct '  ' || (p.address->>'database') || '.'
                    || coalesce(p.address->>'table', p.address->>'view', p.address->>'dictionary'),
                E'\n'
            ) as text
        from
            {{schema}}.ch_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node p on p.id = e.node_tgt_id
        where
            m.role in ('loading', 'target')
        group by
            e.node_src_id, m.role
    ),
    dependents as (
        select
            e.node_tgt_id as rel_id,
            string_agg(
                distinct '  ' || (s.address->>'database') || '.'
                    || coalesce(s.address->>'table', s.address->>'view', s.address->>'dictionary'),
                E'\n'
            ) as text
        from
            {{schema}}.ch_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node s on s.id = e.node_src_id
        where
            m.role = 'dependency'
        group by
            e.node_tgt_id
    )
    select
        x.node_id,
        case x.kind when 'view' then 'View ' else initcap(x.kind) || ' view ' end
            || x.database_name || '.' || x.name
            || coalesce(E'\nEngine: ' || x.engine_full, '')
            || coalesce(E'\nComment: ' || x.comment, '')
            || coalesce(E'\nQuery: ' || x.as_select, '')
            || coalesce(E'\nPartition key: ' || x.partition_key, '')
            || coalesce(E'\nSorting key: ' || x.sorting_key, '')
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nWrites to:\n' || target.text, '')
            || coalesce(E'\nLoads from:\n' || loads.text, '')
            || coalesce(E'\nUsed by:\n' || dependents.text, '') as content
    from
        {{schema}}.ch_meta_view x
        left join cols         on cols.rel_id = x.node_id
        left join links target on target.rel_id = x.node_id and target.role = 'target'
        left join links loads  on loads.rel_id = x.node_id and loads.role = 'loading'
        left join dependents   on dependents.rel_id = x.node_id
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_column — колонка таблицы, представления или словаря. Пример — edge_demo.orders.customer_id:
    meta_name         customer_id
    meta_path         edge_demo.orders.customer_id
    meta_words        customer id
    meta_description  Column edge_demo.orders.customer_id UInt64: Клиент, см. customers
    meta_comment      Клиент, см. customers
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_column', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_column x
    $body$),
    ('ch_meta_column', 'meta_path', $body$
    select
        x.node_id,
        x.database_name || '.' || x.relation_name || '.' || x.name as content
    from
        {{schema}}.ch_meta_column x
    $body$),
    ('ch_meta_column', 'meta_words', $body$
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
        {{schema}}.ch_meta_column x
    $body$),
    ('ch_meta_column', 'meta_description', $body$
    select
        x.node_id,
        'Column ' || x.database_name || '.' || x.relation_name || '.' || x.name
            || ' ' || x.data_type
            || coalesce(' ' || x.default_kind || ' ' || x.default_expression, '')
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.ch_meta_column x
    $body$),
    ('ch_meta_column', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ch_meta_column x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_index — индекс пропуска данных. Пример — edge_demo.orders.idx_status:
    meta_name         idx_status
    meta_path         edge_demo.orders.idx_status
    meta_description  Index idx_status on edge_demo.orders: set(3) on status granularity 4
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_index', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_index x
    $body$),
    ('ch_meta_index', 'meta_path', $body$
    select
        x.node_id,
        x.database_name || '.' || x.table_name || '.' || x.name as content
    from
        {{schema}}.ch_meta_index x
    $body$),
    ('ch_meta_index', 'meta_words', $body$
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
        {{schema}}.ch_meta_index x
    $body$),
    ('ch_meta_index', 'meta_description', $body$
    select
        x.node_id,
        'Index ' || x.name || ' on ' || x.database_name || '.' || x.table_name
            || ': ' || x.kind_full || ' on ' || x.expr || ' granularity ' || x.granularity as content
    from
        {{schema}}.ch_meta_index x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_projection — проекция. Пример — edge_demo.orders.p_by_status:
    meta_name         p_by_status
    meta_path         edge_demo.orders.p_by_status
    meta_description  Projection p_by_status on edge_demo.orders: SELECT status, count() GROUP BY status
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_projection', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_projection x
    $body$),
    ('ch_meta_projection', 'meta_path', $body$
    select
        x.node_id,
        x.database_name || '.' || x.table_name || '.' || x.name as content
    from
        {{schema}}.ch_meta_projection x
    $body$),
    ('ch_meta_projection', 'meta_words', $body$
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
        {{schema}}.ch_meta_projection x
    $body$),
    ('ch_meta_projection', 'meta_description', $body$
    select
        x.node_id,
        'Projection ' || x.name || ' on ' || x.database_name || '.' || x.table_name
            || ': ' || x.query as content
    from
        {{schema}}.ch_meta_projection x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_dictionary — словарь. Пример — edge_demo.dict_customers:
    meta_name         dict_customers
    meta_path         edge_demo.dict_customers
    meta_words        dict customers
    meta_description  Dictionary edge_demo.dict_customers (Hashed): Словарь клиентов. Columns: id (UInt64), name (String), country (String)
    meta_columns      id name country
    meta_describer_input
        Dictionary edge_demo.dict_customers
        Layout: Hashed
        Comment: Словарь клиентов
        Source: ClickHouse: edge_demo.customers
        Lifetime: 60..300 s
        Key: id (UInt64)
        DDL: CREATE DICTIONARY edge_demo.dict_customers (...) PRIMARY KEY id SOURCE(...) LAYOUT(HASHED()) LIFETIME(MIN 60 MAX 300)
        Columns:
          id UInt64
          name String
          country String
        Loads from:
          edge_demo.customers
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_dictionary', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_dictionary x
    $body$),
    ('ch_meta_dictionary', 'meta_path', $body$
    select
        x.node_id,
        x.database_name || '.' || x.name as content
    from
        {{schema}}.ch_meta_dictionary x
    $body$),
    ('ch_meta_dictionary', 'meta_words', $body$
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
        {{schema}}.ch_meta_dictionary x
    $body$),
    ('ch_meta_dictionary', 'meta_description', $body$
    select
        x.node_id,
        'Dictionary ' || x.database_name || '.' || x.name || coalesce(' (' || x.layout || ')', '')
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.ch_meta_dictionary x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ch_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ch_meta_dictionary', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ch_meta_dictionary x
    $body$),
    ('ch_meta_dictionary', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.ch_meta_dictionary x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ch_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ch_meta_dictionary', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || coalesce(' ' || c.default_kind || ' ' || c.default_expression, '')
                    || coalesce(' codec ' || c.codec, '')
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.ch_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    links as (
        select
            e.node_src_id as rel_id,
            m.role::text as role,
            string_agg(
                distinct '  ' || (p.address->>'database') || '.'
                    || coalesce(p.address->>'table', p.address->>'view', p.address->>'dictionary'),
                E'\n'
            ) as text
        from
            {{schema}}.ch_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node p on p.id = e.node_tgt_id
        where
            m.role in ('loading', 'target')
        group by
            e.node_src_id, m.role
    ),
    dependents as (
        select
            e.node_tgt_id as rel_id,
            string_agg(
                distinct '  ' || (s.address->>'database') || '.'
                    || coalesce(s.address->>'table', s.address->>'view', s.address->>'dictionary'),
                E'\n'
            ) as text
        from
            {{schema}}.ch_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node s on s.id = e.node_src_id
        where
            m.role = 'dependency'
        group by
            e.node_tgt_id
    )
    select
        x.node_id,
        'Dictionary ' || x.database_name || '.' || x.name
            || coalesce(E'\nLayout: ' || x.layout, '')
            || coalesce(E'\nComment: ' || x.comment, '')
            || coalesce(E'\nSource: ' || x.source, '')
            || coalesce(E'\nLifetime: ' || x.lifetime_min || '..' || x.lifetime_max || ' s', '')
            || coalesce(E'\nKey: ' || array_to_string(x.key_names, ', ') || ' (' || array_to_string(x.key_types, ', ') || ')', '')
            || coalesce(E'\nDDL: ' || x.create_query, '')
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nLoads from:\n' || loads.text, '')
            || coalesce(E'\nUsed by:\n' || dependents.text, '') as content
    from
        {{schema}}.ch_meta_dictionary x
        left join cols        on cols.rel_id = x.node_id
        left join links loads on loads.rel_id = x.node_id and loads.role = 'loading'
        left join dependents  on dependents.rel_id = x.node_id
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ch_meta_function — SQL-функция пользователя. Пример — edge_demo_amount_rub:
    meta_name         edge_demo_amount_rub
    meta_words        edge demo amount rub
    meta_description  Function edge_demo_amount_rub: CREATE FUNCTION edge_demo_amount_rub AS amount -> (amount * 90)
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ch_meta_function', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ch_meta_function x
    $body$),
    ('ch_meta_function', 'meta_words', $body$
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
        {{schema}}.ch_meta_function x
    $body$),
    ('ch_meta_function', 'meta_description', $body$
    select
        x.node_id,
        'Function ' || x.name || ': ' || x.create_query as content
    from
        {{schema}}.ch_meta_function x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;
