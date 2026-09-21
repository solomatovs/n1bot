/*
cfl-indexer, схема, шаг 4: аспекты поверхностей Confluence.

Индексатор кладёт объекты Confluence в surface-таблицы cfl_* (шаг 2), а поиск работает
не по строкам этих таблиц, а по текстам объекта: заголовку, пути, словам заголовка,
меткам, карточке, полному тексту. Такой текст называется аспектом. Индексаторы ix-fts,
ix-trgm, ix-vector и описатель ix-llm-describer про cfl_* ничего не знают: каждый
подписан на классы аспектов (ident, words, description, describer_input) и берёт тексты
из объявлений {schema}.surface_aspect. Классы аспектов заданы словарём в шаге 1.

Аспекты бывают двух видов. title, path, words, labels, card и describer_input
выводятся запросом из surface-строки и уже лежащего в ix_fts текста. body и ocr
запросом не получить: markdown страницы, текст вложения и распознанную картинку
индексатор кладёт в ix_fts из Python, а объявление читает их оттуда, чтобы тот же
текст дошёл до остальных потребителей. Поверхность cfl_page_link — ребро, аспектов у
неё нет.

Каждый insert объявляет аспекты одной поверхности: строка на пару «поверхность,
аспект», тело — запрос, который отдаёт по строке на node две колонки node_id и content.
Потребитель склеивает тела своих классов в один union all, отбрасывает пустой content и
кладёт тексты в свою таблицу с ключом (node_id, surface, aspect). Для страницы AIP-117
стенда потребитель класса ident получает такие строки:

    surface   aspect  node_id  content
    cfl_page  title   8575     [WIP] AIP-117 Dynamic resolution of task fields
    cfl_page  path    8575     AIRFLOW/[WIP] AIP-117 Dynamic resolution of task fields

Перед каждым insert показано, что его тела отдают для одного объекта стенда с
публичным Confluence Apache; длинный текст обрезан многоточием.

Схема в теле удвоена: после наката в строке остаётся плейсхолдер схемы, его подставит
потребитель. Накат проверяет каждое тело по контракту (node_id, content), повторный
накат перезаписывает тела.
*/

/*
cfl_space — спейс. Пример — AIRFLOW:
    title  Airflow
    words  airflow
    card   Space AIRFLOW: Airflow
           Wiki Home for https://github.com/apache/airflow
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('cfl_space', 'title', $body$
    select
        x.node_id,
        x.name as content
    from
        {{schema}}.cfl_space x
    $body$),
    ('cfl_space', 'words', $body$
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
        {{schema}}.cfl_space x
    $body$),
    ('cfl_space', 'card', $body$
    select
        x.node_id,
        'Space ' || x.space_key || ': ' || x.name
            || coalesce(E'\n' || nullif(x.description, ''), '') as content
    from
        {{schema}}.cfl_space x
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
cfl_page — страница. В card — путь через предков, метки и первые 500 символов текста,
в describer_input — то же с текстом целиком. Пример — AIP-117 из AIRFLOW:
    title   [WIP] AIP-117 Dynamic resolution of task fields
    path    AIRFLOW/[WIP] AIP-117 Dynamic resolution of task fields
    words   [wip] aip 117 dynamic resolution of task fields
    labels  airflow-improvement-proposal/draft airflow-improvement-proposal
    card    Page AIRFLOW/Airflow Wiki/Airflow Improvement Proposals/[WIP] AIP-117 Dynamic resolution of task fields
            Labels: airflow-improvement-proposal/draft airflow-improvement-proposal
            ## Status
            | **State** | Draft |
            …
    body    ## Status
            | **State** | Draft |
            …
    describer_input
            как card, но с полным текстом страницы
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('cfl_page', 'title', $body$
    select
        x.node_id,
        x.title as content
    from
        {{schema}}.cfl_page x
    $body$),
    ('cfl_page', 'path', $body$
    select
        x.node_id,
        x.space_key || '/' || x.title as content
    from
        {{schema}}.cfl_page x
    $body$),
    ('cfl_page', 'words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.title, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.cfl_page x
    $body$),
    ('cfl_page', 'labels', $body$
    select
        x.node_id,
        nullif(array_to_string(x.labels, ' '), '') as content
    from
        {{schema}}.cfl_page x
    $body$),
    ('cfl_page', 'card', $body$
    select
        x.node_id,
        'Page ' || x.space_key || '/' || array_to_string(x.ancestor_titles || x.title, '/')
            || coalesce(E'\nLabels: ' || nullif(array_to_string(x.labels, ' '), ''), '')
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        {{schema}}.cfl_page x
        left join {{schema}}.ix_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_page', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.ix_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_page'
    where
        f.aspect = 'body'
    $body$),
    ('cfl_page', 'describer_input', $body$
    select
        x.node_id,
        'Page ' || x.space_key || '/' || array_to_string(x.ancestor_titles || x.title, '/')
            || coalesce(E'\nLabels: ' || nullif(array_to_string(x.labels, ' '), ''), '')
            || coalesce(E'\n' || f.content, '') as content
    from
        {{schema}}.cfl_page x
        left join {{schema}}.ix_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
cfl_blogpost — запись блога. Предков у неё нет, путь — спейс и заголовок. Пример — запись
AMQCPP без меток, labels пуст и отброшен:
    title  ActiveMQ-CPP 1.1 Released
    path   AMQCPP/ActiveMQ-CPP 1.1 Released
    words  active mq cpp 1.1 released
    card   Blog post AMQCPP/ActiveMQ-CPP 1.1 Released
           The ActiveMQ-CPP 1.1 release is now official! You can download the source …
    body   The ActiveMQ-CPP 1.1 release is now official! You can download the source …
    describer_input
           как card, но с полным текстом записи
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('cfl_blogpost', 'title', $body$
    select
        x.node_id,
        x.title as content
    from
        {{schema}}.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'path', $body$
    select
        x.node_id,
        x.space_key || '/' || x.title as content
    from
        {{schema}}.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.title, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'labels', $body$
    select
        x.node_id,
        nullif(array_to_string(x.labels, ' '), '') as content
    from
        {{schema}}.cfl_blogpost x
    $body$),
    ('cfl_blogpost', 'card', $body$
    select
        x.node_id,
        'Blog post ' || x.space_key || '/' || x.title
            || coalesce(E'\nLabels: ' || nullif(array_to_string(x.labels, ' '), ''), '')
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        {{schema}}.cfl_blogpost x
        left join {{schema}}.ix_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_blogpost', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.ix_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_blogpost'
    where
        f.aspect = 'body'
    $body$),
    ('cfl_blogpost', 'describer_input', $body$
    select
        x.node_id,
        'Blog post ' || x.space_key || '/' || x.title
            || coalesce(E'\nLabels: ' || nullif(array_to_string(x.labels, ' '), ''), '')
            || coalesce(E'\n' || f.content, '') as content
    from
        {{schema}}.cfl_blogpost x
        left join {{schema}}.ix_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
cfl_attachment — вложение. body — текст, извлечённый из документа, ocr — текст, распознанный
на картинке; у файла обычно есть что-то одно, пустое отбрасывается. Пример — pdf и
картинка:
    title  ApacheConUS2007-roller-session-2023.pdf
    path   APACHECON/ApacheConUS2007-roller-session-2023.pdf
    words  apache con us2007 roller session 2023.pdf
    card   Attachment APACHECON/ApacheConUS2007-roller-session-2023.pdf (application/pdf, 2113561 bytes)
           Apache Roller and blogs as a web development platform …
    body   Apache Roller and blogs as a web development platform …

    title  image2019-3-15_12-6-24.png
    path   AIRFLOW/image2019-3-15_12-6-24.png
    words  image2019 3 15 12 6 24.png
    card   Attachment AIRFLOW/image2019-3-15_12-6-24.png (image/png, 50915 bytes)
    ocr    е DAG    Schedule  Owner Recent Tasks @    Last Run @    DAG Runs @ …
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('cfl_attachment', 'title', $body$
    select
        x.node_id,
        x.title as content
    from
        {{schema}}.cfl_attachment x
    $body$),
    ('cfl_attachment', 'path', $body$
    select
        x.node_id,
        x.space_key || '/' || x.title as content
    from
        {{schema}}.cfl_attachment x
    $body$),
    ('cfl_attachment', 'words', $body$
    select
        x.node_id,
        lower(
            replace(
                regexp_replace(
                    regexp_replace(x.title, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
                    '[_\-]+', ' ', 'g'
                ),
                'ё', 'е'
            )
        ) as content
    from
        {{schema}}.cfl_attachment x
    $body$),
    ('cfl_attachment', 'card', $body$
    select
        x.node_id,
        'Attachment ' || x.space_key || '/' || x.title
            || ' (' || x.media_type || ', ' || x.file_size || ' bytes)'
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        {{schema}}.cfl_attachment x
        left join {{schema}}.ix_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_attachment', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.ix_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_attachment'
    where
        f.aspect = 'body'
    $body$),
    ('cfl_attachment', 'ocr', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.ix_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_attachment'
    where
        f.aspect = 'ocr'
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;

/*
cfl_comment — комментарий к странице. Заголовка и пути у него нет, ищется по тексту.
Пример — комментарий в AIRFLOW:
    card  Comment by turbaszek in AIRFLOW
          Issue triage meeting notes: <https://s.apache.org/airflow-triage>
    body  Issue triage meeting notes: <https://s.apache.org/airflow-triage>
*/
insert into {schema}.surface_aspect (surface, aspect, body) values
    ('cfl_comment', 'card', $body$
    select
        x.node_id,
        'Comment by ' || x.author || ' in ' || x.space_key
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        {{schema}}.cfl_comment x
        left join {{schema}}.ix_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_comment', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.ix_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_comment'
    where
        f.aspect = 'body'
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;
