/*
ix-core, схема, шаг 1c: формулы адресов объектов {schema}.surface_url.

У node есть адрес частями (address jsonb), но строка, по которой объект открывает
человек, собирается из них по-разному: страница Confluence это
/pages/viewpage.action?pageId=<id>, спейс это /display/<key>, таблица PostgreSQL это
libpq URI с ролями объекта в query. Знает это владелец поверхности, поэтому он же
кладёт сюда формулу, а потребители (поиск, чат, api) её только применяют и о
происхождениях не знают.

Формула это шаблон из двух знаков:
  {{ключ}}  — подстановка части адреса в percent-кодировке; сверх ключей адреса есть
              {{origin}} — scheme://host с портом, если он не порт схемы;
  [...]     — необязательный кусок: нет в адресе хоть одной подстановки внутри —
              кусок исчезает целиком.
Так одной формулой пишется и префикс пути сервера (Confluence под /confluence и
Confluence в корне), и колонка, которая живёт то в таблице, то в представлении:
?schema={{schema}}[&table={{table}}][&view={{view}}]&column={{column}}.

Поверхности без ссылки строки здесь не имеют: отсутствие строки и значит «ссылки нет».
*/
create table if not exists {schema}.surface_url (
    surface   {schema}.surface_e primary key references {schema}.surface,
    template  varchar not null,
    owner     varchar not null
);
