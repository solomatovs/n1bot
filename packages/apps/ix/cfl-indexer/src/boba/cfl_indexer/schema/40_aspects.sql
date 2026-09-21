/*
cfl-indexer, схема, шаг 4: объявления surface_aspect поверхностей Confluence. Индексатор
сам читает их при записи каждого node: title, path, words, labels и card выводятся из
surface-строки и уже лежащего в cfl_idx_fts текста, а body и ocr он кладёт в cfl_idx_fts
из Python, и объявление читает их оттуда. Схема в теле удвоена, чтобы после наката в
строке остался плейсхолдер; накат проверяет каждое тело по контракту (node_id, content).
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
    $body$),
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
        left join {{schema}}.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_page', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.cfl_idx_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_page'
    where
        f.aspect = 'body'
    $body$),
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
        left join {{schema}}.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_blogpost', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.cfl_idx_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_blogpost'
    where
        f.aspect = 'body'
    $body$),
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
        left join {{schema}}.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_attachment', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.cfl_idx_fts f
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
        {{schema}}.cfl_idx_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_attachment'
    where
        f.aspect = 'ocr'
    $body$),
    ('cfl_comment', 'card', $body$
    select
        x.node_id,
        'Comment by ' || x.author || ' in ' || x.space_key
            || coalesce(E'\n' || nullif(left(f.content, 500), ''), '') as content
    from
        {{schema}}.cfl_comment x
        left join {{schema}}.cfl_idx_fts f
            on  f.node_id = x.node_id
            and f.aspect = 'body'
    $body$),
    ('cfl_comment', 'body', $body$
    select
        f.node_id,
        f.content
    from
        {{schema}}.cfl_idx_fts f
        join {{schema}}.node n
            on  n.id = f.node_id
            and n.surface = 'cfl_comment'
    where
        f.aspect = 'body'
    $body$)
on conflict (surface, aspect) do update
    set body = excluded.body;
