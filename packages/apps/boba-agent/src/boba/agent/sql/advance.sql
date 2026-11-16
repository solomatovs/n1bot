update boba.sessions
set
    processed_input_seq = greatest(processed_input_seq, $2::bigint)
where 1=1
    and session_id = $1::text
returning
    processed_input_seq;
