from __future__ import annotations

import os
import urllib.parse
import urllib.request
from datetime import datetime

from airflow.decorators import dag, task


CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "http://clickhouse:8123")


def clickhouse(sql: str) -> None:
    url = f"{CLICKHOUSE_URL.rstrip('/')}/"
    for statement in (part.strip() for part in sql.split(";")):
        if not statement:
            continue
        request = urllib.request.Request(
            url,
            data=statement.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()


@dag(
    dag_id="bionicpro_reports_etl",
    description="Prepare BionicPRO user reporting mart from CRM and telemetry data.",
    start_date=datetime(2026, 1, 1),
    schedule="0 2 * * *",
    catchup=False,
    tags=["bionicpro", "reports", "olap"],
)
def bionicpro_reports_etl():
    @task
    def create_sources() -> None:
        clickhouse(
            """
            CREATE DATABASE IF NOT EXISTS raw;
            CREATE DATABASE IF NOT EXISTS reporting;

            CREATE TABLE IF NOT EXISTS raw.crm_clients (
                user_id String,
                client_name String,
                email String,
                prosthesis_id String,
                updated_at DateTime
            )
            ENGINE = MergeTree
            ORDER BY (user_id, prosthesis_id);

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

            CREATE TABLE IF NOT EXISTS reporting.user_report_mart (
                user_id String,
                client_name String,
                report_date Date,
                processed_until DateTime,
                events_count UInt64,
                avg_battery_level Float64,
                avg_signal_quality Float64,
                max_temperature Float64,
                total_active_minutes UInt64
            )
            ENGINE = ReplacingMergeTree(processed_until)
            ORDER BY (user_id, report_date);
            """
        )

    @task
    def load_demo_sources() -> None:
        clickhouse(
            """
            TRUNCATE TABLE raw.crm_clients;
            TRUNCATE TABLE raw.prosthesis_telemetry;

            INSERT INTO raw.crm_clients
            SELECT *
            FROM input(
                'user_id String, client_name String, email String, prosthesis_id String, updated_at DateTime'
            )
            FORMAT Values
            ('user1', 'Ivan Petrov', 'user1@example.com', 'prosthesis-001', now()),
            ('user2', 'Anna Smirnova', 'user2@example.com', 'prosthesis-002', now()),
            ('admin1', 'Admin User', 'admin1@example.com', 'prosthesis-003', now());

            INSERT INTO raw.prosthesis_telemetry
            SELECT *
            FROM input(
                'prosthesis_id String, event_time DateTime, battery_level Float64, signal_quality Float64, temperature Float64, active_minutes UInt32'
            )
            FORMAT Values
            ('prosthesis-001', now() - INTERVAL 5 HOUR, 76, 0.94, 36.7, 120),
            ('prosthesis-001', now() - INTERVAL 3 HOUR, 72, 0.91, 37.1, 95),
            ('prosthesis-002', now() - INTERVAL 4 HOUR, 81, 0.89, 36.4, 80),
            ('prosthesis-002', now() - INTERVAL 2 HOUR, 79, 0.92, 36.8, 110),
            ('prosthesis-003', now() - INTERVAL 6 HOUR, 88, 0.97, 36.2, 60),
            ('prosthesis-003', now() - INTERVAL 1 HOUR, 84, 0.95, 36.6, 75);
            """
        )

    @task
    def build_user_report_mart() -> None:
        clickhouse(
            """
            TRUNCATE TABLE reporting.user_report_mart;

            INSERT INTO reporting.user_report_mart
            SELECT
                c.user_id,
                any(c.client_name) AS client_name,
                toDate(t.event_time) AS report_date,
                max(t.event_time) AS processed_until,
                count() AS events_count,
                round(avg(t.battery_level), 2) AS avg_battery_level,
                round(avg(t.signal_quality), 3) AS avg_signal_quality,
                max(t.temperature) AS max_temperature,
                sum(t.active_minutes) AS total_active_minutes
            FROM raw.prosthesis_telemetry t
            INNER JOIN raw.crm_clients c ON c.prosthesis_id = t.prosthesis_id
            WHERE t.event_time < now()
            GROUP BY c.user_id, report_date;
            """
        )

    create_sources() >> load_demo_sources() >> build_user_report_mart()


bionicpro_reports_etl()
