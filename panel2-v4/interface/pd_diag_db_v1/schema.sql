-- Interface two (pd_diag_db/v1): the fixed part of the tables. The ingest process runs this at start and after every
-- start of ClickHouse (pd_ingest.py init); every statement can run again. {SUMMARY_TTL_DAYS} is replaced from
-- clickhouse.env. The segment columns of request_doc (one per segment, names from segment_def) are added by the ingest
-- process when it reads a new config_version; they are not in this file.
-- Every column but doc is computed from doc (MATERIALIZED): the ingest process writes only doc, as the fetcher wrote it.

CREATE DATABASE IF NOT EXISTS vllm_pd;

-- One row per request and document version; queries read it with FINAL (the largest doc_version of a request).
CREATE TABLE IF NOT EXISTS vllm_pd.request_doc
(
    doc String CODEC(ZSTD(3)),
    schema LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'schema'),
    cluster LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'cluster'),
    seq UInt64 MATERIALIZED JSONExtractUInt(doc, 'seq'),
    doc_version UInt32 MATERIALIZED toUInt32(JSONExtractUInt(doc, 'doc_version')),
    closed LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'closed'),
    request_id String MATERIALIZED JSONExtractString(doc, 'request_id'),
    ts DateTime64(3, 'UTC') MATERIALIZED parseDateTime64BestEffort(JSONExtractString(doc, 'ts'), 3, 'UTC'),
    session_id Nullable(String) MATERIALIZED JSONExtract(doc, 'session_id', 'Nullable(String)'),
    client_request_id Nullable(String) MATERIALIZED JSONExtract(doc, 'client_request_id', 'Nullable(String)'),
    model LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'model'),
    route_p LowCardinality(String) MATERIALIZED ifNull(JSONExtract(doc, 'route', 'p', 'Nullable(String)'), ''),
    route_d LowCardinality(String) MATERIALIZED ifNull(JSONExtract(doc, 'route', 'd', 'Nullable(String)'), ''),
    retries Nullable(UInt8) MATERIALIZED JSONExtract(doc, 'route', 'retries', 'Nullable(UInt8)'),
    status LowCardinality(String) MATERIALIZED ifNull(JSONExtract(doc, 'result', 'status', 'Nullable(String)'), ''),
    finish_reason LowCardinality(String) MATERIALIZED ifNull(JSONExtract(doc, 'result', 'finish_reason', 'Nullable(String)'), ''),
    http_status Nullable(UInt16) MATERIALIZED JSONExtract(doc, 'result', 'http_status', 'Nullable(UInt16)'),
    prompt_tokens Nullable(UInt32) MATERIALIZED JSONExtract(doc, 'tokens', 'prompt', 'Nullable(UInt32)'),
    cached_tokens Nullable(UInt32) MATERIALIZED JSONExtract(doc, 'tokens', 'cached', 'Nullable(UInt32)'),
    output_tokens Nullable(UInt32) MATERIALIZED JSONExtract(doc, 'tokens', 'output', 'Nullable(UInt32)'),
    e2e_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'e2e', 'Nullable(Float64)'),
    starts_at LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'latency_ms', 'starts_at'),
    ttft_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'ttft', 'Nullable(Float64)'),
    tpot_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'tpot', 'Nullable(Float64)'),
    p_total_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'p_total', 'Nullable(Float64)'),
    d_total_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'd_total', 'Nullable(Float64)'),
    retry_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'retry_ms', 'Nullable(Float64)'),
    p_e2e_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'official', 'p', 'e2e', 'Nullable(Float64)'),
    p_ttft_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'official', 'p', 'ttft', 'Nullable(Float64)'),
    d_e2e_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'official', 'd', 'e2e', 'Nullable(Float64)'),
    d_ttft_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'latency_ms', 'official', 'd', 'ttft', 'Nullable(Float64)'),
    steps Nullable(UInt32) MATERIALIZED JSONExtract(doc, 'tpot', 'steps', 'Nullable(UInt32)'),
    p_level UInt8 MATERIALIZED toUInt8(JSONExtractUInt(doc, 'diag_level', 'p')),
    d_level UInt8 MATERIALIZED toUInt8(JSONExtractUInt(doc, 'diag_level', 'd')),
    p_recorded_level UInt8 MATERIALIZED toUInt8(JSONExtractUInt(doc, 'recorded_level', 'p')),
    d_recorded_level UInt8 MATERIALIZED toUInt8(JSONExtractUInt(doc, 'recorded_level', 'd')),
    attempts UInt32 MATERIALIZED toUInt32(JSONExtractUInt(doc, 'attempts')),
    p_start Nullable(DateTime64(3, 'UTC')) MATERIALIZED parseDateTime64BestEffortOrNull(JSONExtractString(doc, 'sides', 'p', 'start'), 3, 'UTC'),
    d_start Nullable(DateTime64(3, 'UTC')) MATERIALIZED parseDateTime64BestEffortOrNull(JSONExtractString(doc, 'sides', 'd', 'start'), 3, 'UTC'),
    p_executions Nullable(UInt32) MATERIALIZED JSONExtract(doc, 'sides', 'p', 'executions', 'Nullable(UInt32)'),
    d_executions Nullable(UInt32) MATERIALIZED JSONExtract(doc, 'sides', 'd', 'executions', 'Nullable(UInt32)'),
    p_unattributed_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'checks', 'p_unattributed_ms', 'Nullable(Float64)'),
    d_first_unattributed_ms Nullable(Float64) MATERIALIZED JSONExtract(doc, 'checks', 'd_first_unattributed_ms', 'Nullable(Float64)'),
    missing Array(String) MATERIALIZED JSONExtract(doc, 'missing', 'Array(String)')
)
ENGINE = ReplacingMergeTree(doc_version)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (cluster, request_id)
TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;

-- Status documents: one per instance, proxy worker and the fetcher every minute.
CREATE TABLE IF NOT EXISTS vllm_pd.status_doc
(
    doc String CODEC(ZSTD(3)),
    schema LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'schema'),
    cluster LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'cluster'),
    seq UInt64 MATERIALIZED JSONExtractUInt(doc, 'seq'),
    scope LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'scope'),
    instance LowCardinality(String) MATERIALIZED JSONExtractString(doc, 'instance'),
    ts DateTime64(3, 'UTC') MATERIALIZED parseDateTime64BestEffort(JSONExtractString(doc, 'ts'), 3, 'UTC'),
    side LowCardinality(String) MATERIALIZED ifNull(JSONExtract(doc, 'side', 'Nullable(String)'), ''),
    run_id String MATERIALIZED ifNull(JSONExtract(doc, 'run_id', 'Nullable(String)'), ''),
    config_version String MATERIALIZED ifNull(JSONExtract(doc, 'config_version', 'Nullable(String)'), ''),
    diag_level Nullable(UInt8) MATERIALIZED JSONExtract(doc, 'diag_level', 'Nullable(UInt8)'),
    fetch_errors UInt64 MATERIALIZED JSONExtractUInt(doc, 'fetch', 'errors'),
    fetch_backoffs UInt64 MATERIALIZED JSONExtractUInt(doc, 'fetch', 'backoffs'),
    behind_seconds UInt64 MATERIALIZED JSONExtractUInt(doc, 'fetch', 'behind_seconds'),
    run_changes UInt64 MATERIALIZED JSONExtractUInt(doc, 'fetch', 'run_changes'),
    ring_overwritten_lines UInt64 MATERIALIZED JSONExtractUInt(doc, 'fetch', 'ring_overwritten_lines'),
    rejected_429 UInt64 MATERIALIZED JSONExtractUInt(doc, 'fetch', 'rejected_429'),
    buffer_overwritten UInt64 MATERIALIZED JSONExtractUInt(doc, 'buffer_overwritten'),
    filter_dropped UInt64 MATERIALIZED arraySum(arrayMap(x -> JSONExtractUInt(x, 'dropped'), JSONExtractArrayRaw(doc, 'processes'))),
    filter_errors UInt64 MATERIALIZED arraySum(arrayMap(x -> JSONExtractUInt(x, 'errors') + JSONExtractUInt(x, 'config_errors'), JSONExtractArrayRaw(doc, 'processes'))),
    version_dropped UInt64 MATERIALIZED arraySum(arrayMap(x -> JSONExtractUInt(x, 'version_dropped'), JSONExtractArrayRaw(doc, 'processes'))),
    outlet_dropped_lines UInt64 MATERIALIZED arraySum(arrayMap(x -> JSONExtractUInt(x, 'outlet_dropped_lines'), JSONExtractArrayRaw(doc, 'processes'))),
    open_requests UInt64 MATERIALIZED JSONExtractUInt(doc, 'open_requests'),
    docs_written UInt64 MATERIALIZED JSONExtractUInt(doc, 'docs_written'),
    late_dropped UInt64 MATERIALIZED JSONExtractUInt(doc, 'late_dropped'),
    log_dropped UInt64 MATERIALIZED JSONExtractUInt(doc, 'log_dropped'),
    segment_changes UInt64 MATERIALIZED JSONExtractUInt(doc, 'segment_changes'),
    config_mismatch Array(String) MATERIALIZED JSONExtract(doc, 'config_mismatch', 'Array(String)')
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (cluster, scope, instance, ts)
TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;

-- The segments of each config_version, from the fetcher's segment definition endpoint; one row per segment.
CREATE TABLE IF NOT EXISTS vllm_pd.segment_def
(
    config_version String,
    written_at DateTime('UTC'),
    ord UInt32,
    code String,
    grp LowCardinality(String),
    side LowCardinality(String),
    phase LowCardinality(String),
    parent String,
    depth UInt8,
    from_level UInt8,
    unit LowCardinality(String),
    column_name String,
    doc_path String,
    label String,
    short String,
    proc LowCardinality(String),
    optional UInt8,
    compare UInt8,
    outside_total UInt8,
    parallel UInt8,
    before_proxy UInt8,
    has_src UInt8,
    side_name String,
    side_total_label String
)
ENGINE = ReplacingMergeTree(written_at)
ORDER BY (config_version, column_name);

-- The version table: the major version of interface two that these tables follow. One row.
CREATE TABLE IF NOT EXISTS vllm_pd.db_interface
(
    name String,
    major UInt32,
    written_at DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(written_at)
ORDER BY name;

-- One request's document: SELECT doc FROM vllm_pd.request_json(id = '<request id>')
CREATE OR REPLACE VIEW vllm_pd.request_json AS
SELECT doc FROM vllm_pd.request_doc FINAL WHERE request_id = {id:String};

ALTER TABLE vllm_pd.request_doc MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;
ALTER TABLE vllm_pd.status_doc MODIFY TTL toDate(ts) + INTERVAL {SUMMARY_TTL_DAYS} DAY;
