-- The two accounts of interface two and the ingest process. {CH_WRITER_PASSWORD} and {CH_READER_PASSWORD} are replaced
-- from clickhouse.env (bin/clickhouse_up.sh). vllm_writer: the ingest process (writes documents and segment_def, adds a
-- column per new segment). vllm_reader: Grafana and the board generator, read only.
CREATE SETTINGS PROFILE IF NOT EXISTS vllm_reader_profile SETTINGS max_execution_time = 120, max_memory_usage = 6000000000;
ALTER SETTINGS PROFILE vllm_reader_profile SETTINGS max_execution_time = 120, max_memory_usage = 6000000000, max_bytes_before_external_group_by = 2000000000, join_algorithm = 'auto', max_bytes_in_join = 1000000000;
CREATE USER IF NOT EXISTS vllm_writer;
ALTER USER vllm_writer IDENTIFIED WITH sha256_password BY '{CH_WRITER_PASSWORD}';
GRANT SELECT, INSERT ON vllm_pd.* TO vllm_writer;
GRANT ALTER ADD COLUMN ON vllm_pd.request_doc TO vllm_writer;
GRANT SELECT ON system.columns TO vllm_writer;
GRANT SELECT ON system.tables TO vllm_writer;
CREATE USER IF NOT EXISTS vllm_reader SETTINGS PROFILE 'vllm_reader_profile';
ALTER USER vllm_reader IDENTIFIED WITH sha256_password BY '{CH_READER_PASSWORD}';
GRANT SELECT ON vllm_pd.* TO vllm_reader;
