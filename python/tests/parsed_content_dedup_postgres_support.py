"""Shared setup for parsed content dedup postgres tests."""

from __future__ import annotations

import psycopg


def _replay_job(
    connection: psycopg.Connection, observation_id: int, parser_version: str
) -> int:
    return connection.execute(
        """
        INSERT INTO python_processing_jobs (
            replay_observation_id, work_type, deduplication_key, input_json,
            parser_version, processing_version, domain_rule_version,
            analytics_rule_version
        ) VALUES (
            %s, 'replay_observation', %s, '{\"replay_request_id\": 1}'::jsonb,
            %s, 'clashlens-domain-processing-v1', 'clashlens-domain-rules-v1',
            'legend-analytics-v1'
        )
        RETURNING id
        """,
        (
            observation_id,
            f"dedup-replay:{observation_id}:{parser_version}",
            parser_version,
        ),
    ).fetchone()[0]
