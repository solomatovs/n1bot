-- @max 90199
create table dm.bookings (
    id      bigserial primary key,
    room    int       not null,
    area    box       not null,
    exclude using gist (area with &&)
);
