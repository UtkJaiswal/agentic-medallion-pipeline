"""Deterministic column profiler over bronze (schema-on-read jsonb). Runs in SQL so it scales with the
database rather than Python memory; on very large tables it profiles a TABLESAMPLE.

Output feeds three consumers: the Data Quality Agent (as its only view of the data), metadata
auto-tagging at landing, and drift baselines between runs."""

from __future__ import annotations

from typing import Any

import psycopg

from medallion.pipeline.silver.parsers import NULL_TOKENS

SAMPLE_ABOVE_ROWS = 1_000_000

_SHAPE = "regexp_replace(regexp_replace(v, '[0-9]', '9', 'g'), '[A-Za-z]', 'a', 'g')"


def _source(conn: psycopg.Connection, column: str) -> tuple[str, dict[str, Any]]:
    total = conn.execute("SELECT count(*) AS n FROM bronze.tickets_raw").fetchone()["n"]
    sample = " TABLESAMPLE SYSTEM (5)" if total > SAMPLE_ABOVE_ROWS else ""
    return f"SELECT payload->>%(col)s AS v FROM bronze.tickets_raw{sample}", {"col": column, "sampled": bool(sample)}


def profile_column(conn: psycopg.Connection, column: str) -> dict[str, Any]:
    src, meta = _source(conn, column)
    params = {"col": column, "nulls": sorted(NULL_TOKENS)}
    base = conn.execute(f"""
        WITH v AS ({src})
        SELECT count(*) AS total,
               count(*) FILTER (WHERE v IS NULL OR btrim(v) = '') AS empty,
               count(*) FILTER (WHERE lower(btrim(v)) = ANY(%(nulls)s) AND btrim(v) <> '') AS placeholder,
               count(DISTINCT v) AS distinct_raw,
               count(DISTINCT lower(btrim(v))) AS distinct_normalised,
               count(*) FILTER (WHERE v <> btrim(v)) AS whitespace_padded,
               round(avg(length(v))::numeric, 1) AS avg_length, max(length(v)) AS max_length,
               count(*) FILTER (WHERE v ~ '^\\$?-?[0-9,]+(\\.[0-9]+)?$') AS numeric_like,
               count(*) FILTER (WHERE v ~ '^\\$') AS currency_prefixed
        FROM v""", params).fetchone()
    top = conn.execute(f"""WITH v AS ({src}) SELECT v AS value, count(*) AS n FROM v
                           GROUP BY v ORDER BY n DESC, v LIMIT 15""", params).fetchall()
    shapes = conn.execute(f"""WITH v AS ({src}) SELECT {_SHAPE} AS shape, count(*) AS n, min(v) AS example
                              FROM v WHERE v IS NOT NULL AND v <> '' GROUP BY 1 ORDER BY n DESC LIMIT 12""",
                          params).fetchall()
    profile: dict[str, Any] = {**dict(base), **meta, "top_values": [dict(r) for r in top],
                               "shapes": [dict(r) for r in shapes]}
    if base["numeric_like"] and base["numeric_like"] >= 0.3 * max(1, base["total"] - base["empty"]):
        stats = conn.execute(f"""
            WITH v AS ({src}), n AS (SELECT replace(replace(v, '$', ''), ',', '')::numeric AS x FROM v
                                     WHERE v ~ '^\\$?-?[0-9,]+(\\.[0-9]+)?$')
            SELECT min(x) AS min, max(x) AS max, count(*) FILTER (WHERE x < 0) AS negative,
                   count(*) FILTER (WHERE x = 0) AS zero,
                   percentile_cont(ARRAY[0.01, 0.5, 0.99]) WITHIN GROUP (ORDER BY x) AS p01_p50_p99
            FROM n""", params).fetchone()
        profile["numeric"] = dict(stats)
    if any("99/99/9999" in s["shape"] or "99-99-9999" in s["shape"] for s in shapes):
        # Evidence for day/month order in ambiguous dates: if one position never exceeds 12, it is the month.
        dm = conn.execute(f"""
            WITH v AS ({src}), p AS (SELECT regexp_match(v, '^(\\d\\d)[/-](\\d\\d)[/-]\\d{{4}}') AS m FROM v)
            SELECT count(*) FILTER (WHERE m[1]::int > 12) AS first_part_gt_12,
                   count(*) FILTER (WHERE m[2]::int > 12) AS second_part_gt_12,
                   count(*) FILTER (WHERE m IS NOT NULL) AS ambiguous_candidates
            FROM p""", params).fetchone()
        profile["day_month_evidence"] = dict(dm)
    return profile


def profile_table(conn: psycopg.Connection, columns: list[str]) -> dict[str, Any]:
    columns_profile = {c: profile_column(conn, c) for c in columns}
    cross = conn.execute("""
        SELECT count(*) AS total_rows,
               count(*) - count(DISTINCT payload->>'ticket_id') AS duplicate_ticket_ids,
               count(*) - count(DISTINCT row_hash) AS exact_duplicate_rows,
               count(*) - count(DISTINCT (payload - 'ticket_id' - 'created_at' - 'submitted_by')::text)
                   AS duplicates_ignoring_id_created_submitter
        FROM bronze.tickets_raw""").fetchone()
    return {"columns": columns_profile, "table": dict(cross)}
