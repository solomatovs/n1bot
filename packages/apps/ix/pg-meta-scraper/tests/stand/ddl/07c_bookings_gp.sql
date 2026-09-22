create table dm.bookings (
    id      bigserial primary key,
    room    int       not null,
    during  tstzrange not null
) distributed by (id);
