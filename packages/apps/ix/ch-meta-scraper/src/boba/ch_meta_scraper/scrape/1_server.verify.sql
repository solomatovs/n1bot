select
    version() as version,
    hex(sipHash64(version())) as row_version
