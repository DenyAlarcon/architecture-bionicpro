CREATE DATABASE IF NOT EXISTS cdc;
CREATE DATABASE IF NOT EXISTS raw;
CREATE DATABASE IF NOT EXISTS reporting;

CREATE TABLE IF NOT EXISTS raw.prosthesis_telemetry (
    prosthesis_id String,
    event_time DateTime,
    battery_level Float64,
    signal_quality Float64,
    temperature Float64,
    active_minutes UInt32
)
ENGINE = MergeTree
ORDER BY (prosthesis_id, event_time);

CREATE TABLE IF NOT EXISTS cdc.crm_clients_kafka (
    user_id String,
    client_name String,
    email String,
    prosthesis_id String,
    updated_at String,
    `__op` String,
    `__source_ts_ms` UInt64
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'crm.public.crm_clients',
    kafka_group_name = 'clickhouse-crm-clients',
    kafka_format = 'JSONEachRow',
    kafka_num_consumers = 1,
    kafka_handle_error_mode = 'stream';

CREATE TABLE IF NOT EXISTS cdc.crm_clients_current (
    user_id String,
    client_name String,
    email String,
    prosthesis_id String,
    updated_at Nullable(DateTime),
    is_deleted UInt8,
    cdc_ts DateTime64(3)
)
ENGINE = ReplacingMergeTree(cdc_ts)
ORDER BY (user_id, prosthesis_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS cdc.crm_clients_from_kafka_mv
TO cdc.crm_clients_current
AS
SELECT
    user_id,
    client_name,
    email,
    prosthesis_id,
    parseDateTimeBestEffortOrNull(updated_at) AS updated_at,
    if(`__op` = 'd', 1, 0) AS is_deleted,
    fromUnixTimestamp64Milli(`__source_ts_ms`) AS cdc_ts
FROM cdc.crm_clients_kafka;

CREATE TABLE IF NOT EXISTS reporting.user_report_mart_cdc (
    user_id String,
    client_name String,
    report_date Date,
    processed_until DateTime,
    report_version DateTime64(3),
    events_count UInt64,
    avg_battery_level Float64,
    avg_signal_quality Float64,
    max_temperature Float64,
    total_active_minutes UInt64
)
ENGINE = ReplacingMergeTree(report_version)
ORDER BY (user_id, report_date);

CREATE MATERIALIZED VIEW IF NOT EXISTS reporting.user_report_mart_cdc_mv
TO reporting.user_report_mart_cdc
AS
SELECT
    c.user_id,
    any(c.client_name) AS client_name,
    toDate(t.event_time) AS report_date,
    max(t.event_time) AS processed_until,
    max(c.cdc_ts) AS report_version,
    count() AS events_count,
    round(avg(t.battery_level), 2) AS avg_battery_level,
    round(avg(t.signal_quality), 3) AS avg_signal_quality,
    max(t.temperature) AS max_temperature,
    sum(t.active_minutes) AS total_active_minutes
FROM cdc.crm_clients_current AS c
INNER JOIN raw.prosthesis_telemetry AS t ON c.prosthesis_id = t.prosthesis_id
WHERE c.is_deleted = 0 AND t.event_time < now()
GROUP BY c.user_id, report_date;
