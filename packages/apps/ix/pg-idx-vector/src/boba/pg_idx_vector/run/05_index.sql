/*
pg-idx-vector, шаг 0: частичный HNSW на одну пару surface + aspect из объявлений.
Имя индекса, поверхность и аспект подставляет воркер; повторный вызов ничего не делает.
*/
-- @name index
create index if not exists {index_name}
    on {schema}.pg_idx_emb_e5_1024 using hnsw (emb halfvec_cosine_ops)
    where surface = {surface} and aspect = {aspect};
