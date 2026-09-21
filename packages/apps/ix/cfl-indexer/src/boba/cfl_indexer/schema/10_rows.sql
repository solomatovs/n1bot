/*
cfl-indexer, схема, шаг 1: строки словарей {schema}.surface и {schema}.aspect.
*/
insert into {schema}.surface (name, description) values
    ('cfl_space',      'Спейс Confluence; корень его tree.'),
    ('cfl_page',       'Страница Confluence.'),
    ('cfl_blogpost',   'Запись блога спейса Confluence.'),
    ('cfl_attachment', 'Файл, вложенный в страницу или блог-запись.'),
    ('cfl_comment',    'Встроенный или нижний комментарий к странице.'),
    ('cfl_page_link',  'Ребро: ссылка со страницы на другую страницу Confluence.')
on conflict (name) do nothing;

insert into {schema}.aspect (aspect, class, description, owner) values
    ('title',  'ident',       'Заголовок страницы, имя файла вложения или имя спейса как есть.',          'cfl-indexer'),
    ('path',   'ident',       'Путь через ключ спейса: DEV/Заголовок, DEV/Заголовок/design.pdf.',          'cfl-indexer'),
    ('words',  'words',       'Слова заголовка по CamelCase и подчёркиваниям, в нижнем регистре, ё -> е.',  'cfl-indexer'),
    ('labels', 'description', 'Метки страницы через пробел.',                                             'cfl-indexer'),
    ('card',   'description', 'Карточка: вид объекта, путь, метки и начало текста.',                     'cfl-indexer'),
    ('body',   'description', 'Полный текст: markdown страницы, текст вложения или комментария.',        'cfl-indexer'),
    ('ocr',    'description', 'Текст, распознанный на картинке или скане вложения.',                     'cfl-indexer'),
    ('describer_input', 'describer_input', 'Страница целиком для описателя: заголовок, путь, метки и текст.', 'cfl-indexer')
on conflict (aspect) do nothing;
