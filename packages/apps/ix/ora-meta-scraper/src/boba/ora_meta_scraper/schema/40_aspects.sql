/*
ora-meta-scraper, схема, шаг 4: аспекты поверхностей ora_meta_*.

Скрапер кладёт объекты Oracle в surface-таблицы ora_meta_* (шаг 3), а поиск работает
не по строкам этих таблиц, а по текстам объекта: имени, пути, словам имени, описанию,
карточке для модели. Индексаторы и описатель про ora_meta_* ничего не знают: каждый
подписан на классы аспектов и берёт тексты из объявлений {schema}.surface_aspect.
Имена аспектов те же, что у pg-meta-scraper и ch-meta-scraper, словарь общий: строки
уже могут быть вписаны ими, тогда insert ничего не меняет.

Каждое тело отдаёт по строке на node две колонки node_id и content; пустой content
потребитель отбрасывает. Схема в теле удвоена: после наката в строке остаётся
плейсхолдер схемы, его подставит потребитель. Накат проверяет каждое тело по контракту.
*/

insert into {schema}.aspect (aspect, class, description, owner) values
    ('meta_name',            'ident',           'Имя объекта как есть: точное совпадение и префикс.',                                                        'ora-meta-scraper'),
    ('meta_path',            'ident',           'Путь через точку, как пишет пользователь: schema.table или schema.table.column.',                          'ora-meta-scraper'),
    ('meta_words',           'words',           'Слова имени, разрезанного по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е.',                      'ora-meta-scraper'),
    ('meta_description',     'description',     'Описание объекта из всего, что о нём известно скраперу: заголовок, вид, комментарий, колонки с типами.',   'ora-meta-scraper'),
    ('meta_comment',         'description',     'Комментарий из источника как есть.',                                                                        'ora-meta-scraper'),
    ('meta_columns',         'description',     'Имена колонок таблицы, представления или mview через пробел: отношение находится по своим колонкам.',      'ora-meta-scraper'),
    ('meta_describer_input', 'describer_input', 'Структура таблицы, представления или mview для описателя: колонки, ограничения, индексы, связи.',           'ora-meta-scraper')
on conflict (aspect) do nothing;


/*
ora_meta_database — база, корень источника. Пример — orclpdb1.localdomain на 12.2:
    meta_name         orclpdb1.localdomain
    meta_description  Oracle database orclpdb1.localdomain (ORCLPDB1) version 12.2.0.1.0
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_database', 'meta_name', $body$
    select
        x.node_id,
        x.service as content
    from
        {{schema}}.ora_meta_database x
    $body$),
    ('ora_meta_database', 'meta_words', $body$
    select
        x.node_id,
        lower(replace(replace(x.service, '.', ' '), '_', ' ')) as content
    from
        {{schema}}.ora_meta_database x
    $body$),
    ('ora_meta_database', 'meta_description', $body$
    select
        x.node_id,
        'Oracle database ' || x.service
            || coalesce(' (' || x.con_name || ')', '')
            || coalesce(' version ' || x.version, '') as content
    from
        {{schema}}.ora_meta_database x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ora_meta_schema — схема. Пример — EDGE_DEMO:
    meta_name         EDGE_DEMO
    meta_words        edge demo
    meta_description  Schema EDGE_DEMO
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_schema', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_schema x
    $body$),
    ('ora_meta_schema', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_schema x
    $body$),
    ('ora_meta_schema', 'meta_description', $body$
    select
        x.node_id,
        'Schema ' || x.name as content
    from
        {{schema}}.ora_meta_schema x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ora_meta_table — таблица. meta_describer_input — карточка для описателя: колонки,
ограничения, индексы, ключ партиционирования, кто зависит. Пример — EDGE_DEMO.ORDERS:
    meta_name         ORDERS
    meta_path         EDGE_DEMO.ORDERS
    meta_words        orders
    meta_description  Table EDGE_DEMO.ORDERS: Заказы. Columns: ID (NUMBER), CUSTOMER_ID (NUMBER), ...
    meta_columns      ID CUSTOMER_ID AMOUNT STATUS CREATED_AT PAYLOAD
    meta_describer_input
        Table EDGE_DEMO.ORDERS
        Comment: Заказы
        Rows: 0
        Partitioned: RANGE by CREATED_AT
        Columns:
          ID NUMBER(12) not null
          CUSTOMER_ID NUMBER(10) not null -- Клиент, см. customers
          ...
        Constraints:
          ORDERS_PK primary key (ID)
          ORDERS_CUSTOMER_FK foreign key (CUSTOMER_ID) references EDGE_DEMO.CUSTOMERS (ID) on delete CASCADE
          ORDERS_AMOUNT_CK check amount >= 0
        Indexes:
          ORDERS_CUSTOMER_IX NORMAL (CUSTOMER_ID, CREATED_AT DESC)
        Used by:
          EDGE_DEMO.CUSTOMER_ORDERS
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_table', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_table x
    $body$),
    ('ora_meta_table', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_table x
    $body$),
    ('ora_meta_table', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_table x
    $body$),
    ('ora_meta_table', 'meta_description', $body$
    select
        x.node_id,
        'Table ' || x.schema_name || '.' || x.name
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.ora_meta_table x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ora_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ora_meta_table', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ora_meta_table x
    $body$),
    ('ora_meta_table', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.ora_meta_table x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names
            from
                {{schema}}.ora_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ora_meta_table', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || case
                        when c.data_type in ('VARCHAR2', 'NVARCHAR2', 'CHAR', 'NCHAR', 'RAW')
                            then '(' || c.data_length || ')'
                        when c.data_type = 'NUMBER' and c.data_precision is not null
                            then '(' || c.data_precision || coalesce(',' || c.data_scale, '') || ')'
                        else ''
                    end
                    || case when c.nullable then '' else ' not null' end
                    || case when c.virtual then ' virtual' else '' end
                    || case when c.identity then ' identity' else '' end
                    || coalesce(' default ' || c.default_text, '')
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.ora_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    con_cols as (
        select
            e.node_src_id as con_id,
            m.side,
            string_agg(n.address->>'column', ', ' order by m.ordinal) as names
        from
            {{schema}}.ora_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node n on n.id = e.node_tgt_id
        where
            m.role = 'constraint'
        group by
            e.node_src_id, m.side
    ),
    cons as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || k.name || ' '
                    || case k.kind
                        when 'P' then 'primary key (' || coalesce(own.names, '') || ')'
                        when 'U' then 'unique (' || coalesce(own.names, '') || ')'
                        when 'R' then 'foreign key (' || coalesce(own.names, '') || ') references '
                            || coalesce(k.ref_schema || '.' || (rt.address->>'table'), k.ref_constraint)
                            || ' (' || coalesce(ref.names, '') || ')'
                            || coalesce(' on delete ' || nullif(k.delete_rule, 'NO ACTION'), '')
                        when 'C' then 'check ' || coalesce(k.search_condition, '')
                        else k.kind
                    end
                    || case when k.enabled then '' else ' disabled' end,
                E'\n' order by k.kind, k.name
            ) as text
        from
            {{schema}}.ora_meta_constraint k
            join {{schema}}.tree tr on tr.node_id = k.node_id
            left join con_cols own on own.con_id = k.node_id and own.side = 0
            left join con_cols ref on ref.con_id = k.node_id and ref.side = 1
            left join lateral (
                select p.address
                from
                    {{schema}}.ora_meta_edge m
                    join {{schema}}.edge e on e.id = m.edge_id
                    join {{schema}}.tree ct on ct.node_id = e.node_tgt_id
                    join {{schema}}.node p on p.id = ct.parent_id
                where
                    m.role = 'constraint' and m.side = 1 and e.node_src_id = k.node_id
                limit 1
            ) rt on true
        group by
            tr.parent_id
    ),
    idx as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || i.name || ' ' || coalesce(i.index_type, '')
                    || case when i.is_unique then ' unique' else '' end
                    || coalesce(' (' || i.columns || ')', ''),
                E'\n' order by i.name
            ) as text
        from
            {{schema}}.ora_meta_index i
            join {{schema}}.tree tr on tr.node_id = i.node_id
        group by
            tr.parent_id
    ),
    part as (
        select
            e.node_src_id as rel_id,
            string_agg(n.address->>'column', ', ' order by m.ordinal) as names
        from
            {{schema}}.ora_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node n on n.id = e.node_tgt_id
        where
            m.role = 'partition_key'
        group by
            e.node_src_id
    ),
    dependents as (
        select
            e.node_tgt_id as rel_id,
            string_agg(
                distinct '  ' || (s.address->>'schema') || '.'
                    || coalesce(s.address->>'table', s.address->>'view', s.address->>'mview',
                                s.address->>'routine', s.address->>'synonym'),
                E'\n'
            ) as text
        from
            {{schema}}.ora_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node s on s.id = e.node_src_id
        where
            m.role in ('dependency', 'synonym')
        group by
            e.node_tgt_id
    )
    select
        x.node_id,
        'Table ' || x.schema_name || '.' || x.name
            || coalesce(E'\nComment: ' || x.comment, '')
            || coalesce(E'\nRows: ' || x.num_rows, '')
            || case when x.iot then E'\nIndex-organized table' else '' end
            || case when x.temporary then E'\nGlobal temporary table' else '' end
            || case
                when x.partitioned
                    then E'\nPartitioned: ' || coalesce(x.partition_type, '') || coalesce(' by ' || part.names, '')
                else ''
            end
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nConstraints:\n' || cons.text, '')
            || coalesce(E'\nIndexes:\n' || idx.text, '')
            || coalesce(E'\nUsed by:\n' || dependents.text, '') as content
    from
        {{schema}}.ora_meta_table x
        left join cols on cols.rel_id = x.node_id
        left join cons on cons.rel_id = x.node_id
        left join idx on idx.rel_id = x.node_id
        left join part on part.rel_id = x.node_id
        left join dependents on dependents.rel_id = x.node_id
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ora_meta_view — представление. Пример — EDGE_DEMO.CUSTOMER_ORDERS:
    meta_name         CUSTOMER_ORDERS
    meta_path         EDGE_DEMO.CUSTOMER_ORDERS
    meta_description  View EDGE_DEMO.CUSTOMER_ORDERS: Заказы по клиентам. Columns: CUSTOMER_ID (NUMBER), ...
    meta_describer_input
        View EDGE_DEMO.CUSTOMER_ORDERS
        Comment: Заказы по клиентам
        Query: select c.id as customer_id, ...
        Columns:
          CUSTOMER_ID NUMBER(10)
        Depends on:
          EDGE_DEMO.CUSTOMERS
          EDGE_DEMO.ORDERS
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_view', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_view x
    $body$),
    ('ora_meta_view', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_view x
    $body$),
    ('ora_meta_view', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_view x
    $body$),
    ('ora_meta_view', 'meta_description', $body$
    select
        x.node_id,
        'View ' || x.schema_name || '.' || x.name
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.ora_meta_view x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ora_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ora_meta_view', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ora_meta_view x
    $body$),
    ('ora_meta_view', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.ora_meta_view x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names
            from
                {{schema}}.ora_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ora_meta_view', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.ora_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    depends as (
        select
            e.node_src_id as rel_id,
            string_agg(
                distinct '  ' || (p.address->>'schema') || '.'
                    || coalesce(p.address->>'table', p.address->>'view', p.address->>'mview',
                                p.address->>'routine', p.address->>'synonym', p.address->>'sequence'),
                E'\n'
            ) as text
        from
            {{schema}}.ora_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node p on p.id = e.node_tgt_id
        where
            m.role = 'dependency'
        group by
            e.node_src_id
    )
    select
        x.node_id,
        'View ' || x.schema_name || '.' || x.name
            || coalesce(E'\nComment: ' || x.comment, '')
            || coalesce(E'\nQuery: ' || x.text, '')
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nDepends on:\n' || depends.text, '') as content
    from
        {{schema}}.ora_meta_view x
        left join cols on cols.rel_id = x.node_id
        left join depends on depends.rel_id = x.node_id
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ora_meta_mview — материализованное представление. Пример — EDGE_DEMO.DAILY_SALES:
    meta_name         DAILY_SALES
    meta_path         EDGE_DEMO.DAILY_SALES
    meta_description  Materialized view EDGE_DEMO.DAILY_SALES: Продажи по дням. Columns: SALE_DAY (DATE), ...
    meta_describer_input
        Materialized view EDGE_DEMO.DAILY_SALES
        Comment: Продажи по дням
        Refresh: DEMAND
        Query: select trunc(o.created_at) as sale_day, ...
        Columns:
          SALE_DAY DATE
        Indexes:
          DAILY_SALES_DAY_IX NORMAL (SALE_DAY)
        Depends on:
          EDGE_DEMO.ORDERS
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_mview', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_mview x
    $body$),
    ('ora_meta_mview', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_mview x
    $body$),
    ('ora_meta_mview', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_mview x
    $body$),
    ('ora_meta_mview', 'meta_description', $body$
    select
        x.node_id,
        'Materialized view ' || x.schema_name || '.' || x.name
            || coalesce(': ' || x.comment, '')
            || coalesce('. Columns: ' || col.typed, '') as content
    from
        {{schema}}.ora_meta_mview x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(
                    c.name || ' (' || c.data_type || ')', ', ' order by c.ordinal
                ) as typed
            from
                {{schema}}.ora_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ora_meta_mview', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ora_meta_mview x
    $body$),
    ('ora_meta_mview', 'meta_columns', $body$
    select
        x.node_id,
        col.names as content
    from
        {{schema}}.ora_meta_mview x
        left join (
            select
                t.parent_id as rel_id,
                string_agg(c.name, ' ' order by c.ordinal) as names
            from
                {{schema}}.ora_meta_column c
                join {{schema}}.tree t on t.node_id = c.node_id
            group by
                t.parent_id
        ) col on col.rel_id = x.node_id
    $body$),
    ('ora_meta_mview', 'meta_describer_input', $body$
    with cols as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || c.name || ' ' || c.data_type
                    || coalesce(' -- ' || c.comment, ''),
                E'\n' order by c.ordinal
            ) as text
        from
            {{schema}}.ora_meta_column c
            join {{schema}}.tree tr on tr.node_id = c.node_id
        group by
            tr.parent_id
    ),
    idx as (
        select
            tr.parent_id as rel_id,
            string_agg(
                '  ' || i.name || ' ' || coalesce(i.index_type, '')
                    || case when i.is_unique then ' unique' else '' end
                    || coalesce(' (' || i.columns || ')', ''),
                E'\n' order by i.name
            ) as text
        from
            {{schema}}.ora_meta_index i
            join {{schema}}.tree tr on tr.node_id = i.node_id
        group by
            tr.parent_id
    ),
    depends as (
        select
            e.node_src_id as rel_id,
            string_agg(
                distinct '  ' || (p.address->>'schema') || '.'
                    || coalesce(p.address->>'table', p.address->>'view', p.address->>'mview',
                                p.address->>'routine', p.address->>'synonym', p.address->>'sequence'),
                E'\n'
            ) as text
        from
            {{schema}}.ora_meta_edge m
            join {{schema}}.edge e on e.id = m.edge_id
            join {{schema}}.node p on p.id = e.node_tgt_id
        where
            m.role = 'dependency'
        group by
            e.node_src_id
    )
    select
        x.node_id,
        'Materialized view ' || x.schema_name || '.' || x.name
            || coalesce(E'\nComment: ' || x.comment, '')
            || coalesce(E'\nRefresh: ' || x.refresh_mode, '')
            || coalesce(E'\nQuery: ' || x.query, '')
            || coalesce(E'\nColumns:\n' || cols.text, '')
            || coalesce(E'\nIndexes:\n' || idx.text, '')
            || coalesce(E'\nDepends on:\n' || depends.text, '') as content
    from
        {{schema}}.ora_meta_mview x
        left join cols on cols.rel_id = x.node_id
        left join idx on idx.rel_id = x.node_id
        left join depends on depends.rel_id = x.node_id
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ora_meta_column — колонка. Пример — EDGE_DEMO.ORDERS.CUSTOMER_ID:
    meta_name         CUSTOMER_ID
    meta_path         EDGE_DEMO.ORDERS.CUSTOMER_ID
    meta_words        customer id
    meta_description  Column CUSTOMER_ID (NUMBER) of table EDGE_DEMO.ORDERS: Клиент, см. customers
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_column', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_column x
    $body$),
    ('ora_meta_column', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.relation_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_column x
    $body$),
    ('ora_meta_column', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_column x
    $body$),
    ('ora_meta_column', 'meta_description', $body$
    select
        x.node_id,
        'Column ' || x.name || ' (' || x.data_type || ') of ' || x.relation_kind || ' '
            || x.schema_name || '.' || x.relation_name
            || coalesce(': ' || x.comment, '') as content
    from
        {{schema}}.ora_meta_column x
    $body$),
    ('ora_meta_column', 'meta_comment', $body$
    select
        x.node_id,
        x.comment as content
    from
        {{schema}}.ora_meta_column x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ora_meta_constraint, ora_meta_index, ora_meta_trigger — объекты под таблицей: имя, путь
через таблицу, слова, короткое описание.
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_constraint', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_constraint x
    $body$),
    ('ora_meta_constraint', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.table_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_constraint x
    $body$),
    ('ora_meta_constraint', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_constraint x
    $body$),
    ('ora_meta_constraint', 'meta_description', $body$
    select
        x.node_id,
        case x.kind
            when 'P' then 'Primary key '
            when 'U' then 'Unique constraint '
            when 'R' then 'Foreign key '
            when 'C' then 'Check constraint '
            else 'Constraint '
        end
            || x.name || ' of ' || x.schema_name || '.' || x.table_name
            || coalesce(' references ' || x.ref_schema || '.' || x.ref_constraint, '')
            || coalesce(': ' || x.search_condition, '') as content
    from
        {{schema}}.ora_meta_constraint x
    $body$),
    ('ora_meta_index', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_index x
    $body$),
    ('ora_meta_index', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.table_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_index x
    $body$),
    ('ora_meta_index', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_index x
    $body$),
    ('ora_meta_index', 'meta_description', $body$
    select
        x.node_id,
        'Index ' || x.name || coalesce(' (' || x.index_type || ')', '')
            || case when x.is_unique then ' unique' else '' end
            || ' on ' || x.schema_name || '.' || x.table_name
            || coalesce(' (' || x.columns || ')', '') as content
    from
        {{schema}}.ora_meta_index x
    $body$),
    ('ora_meta_trigger', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_trigger x
    $body$),
    ('ora_meta_trigger', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.table_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_trigger x
    $body$),
    ('ora_meta_trigger', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_trigger x
    $body$),
    ('ora_meta_trigger', 'meta_description', $body$
    select
        x.node_id,
        'Trigger ' || x.name || ' ' || coalesce(x.trigger_type, '') || ' ' || coalesce(x.event, '')
            || ' on ' || x.schema_name || '.' || x.table_name
            || case when x.enabled then '' else ' (disabled)' end as content
    from
        {{schema}}.ora_meta_trigger x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
ora_meta_sequence, ora_meta_synonym, ora_meta_routine — объекты схемы: имя, путь,
слова, короткое описание.
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('ora_meta_sequence', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_sequence x
    $body$),
    ('ora_meta_sequence', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_sequence x
    $body$),
    ('ora_meta_sequence', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_sequence x
    $body$),
    ('ora_meta_sequence', 'meta_description', $body$
    select
        x.node_id,
        'Sequence ' || x.schema_name || '.' || x.name
            || coalesce(' increment by ' || x.increment_by, '')
            || coalesce(' cache ' || x.cache_size, '')
            || case when x.cycle then ' cycle' else '' end as content
    from
        {{schema}}.ora_meta_sequence x
    $body$),
    ('ora_meta_synonym', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_synonym x
    $body$),
    ('ora_meta_synonym', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_synonym x
    $body$),
    ('ora_meta_synonym', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_synonym x
    $body$),
    ('ora_meta_synonym', 'meta_description', $body$
    select
        x.node_id,
        'Synonym ' || x.schema_name || '.' || x.name || ' for '
            || coalesce(x.target_schema || '.', '') || x.target_name
            || coalesce('@' || x.db_link, '') as content
    from
        {{schema}}.ora_meta_synonym x
    $body$),
    ('ora_meta_routine', 'meta_name', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.ora_meta_routine x
    $body$),
    ('ora_meta_routine', 'meta_path', $body$
    select
        x.node_id,
        x.schema_name || '.' || x.name as content
    from
        {{schema}}.ora_meta_routine x
    $body$),
    ('ora_meta_routine', 'meta_words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.name, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-$#]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.ora_meta_routine x
    $body$),
    ('ora_meta_routine', 'meta_description', $body$
    select
        x.node_id,
        initcap(x.kind) || ' ' || x.schema_name || '.' || x.name
            || coalesce(' (' || nullif(x.status, 'VALID') || ')', '') as content
    from
        {{schema}}.ora_meta_routine x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;
