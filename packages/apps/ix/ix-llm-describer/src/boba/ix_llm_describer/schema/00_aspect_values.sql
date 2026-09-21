/*
ix-llm-describer, схема, шаг 0: значение aspect_e, которым владеет описатель.
*/
alter type {schema}.aspect_e add value if not exists 'llm_description';
