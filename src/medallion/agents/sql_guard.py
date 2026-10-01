"""Guardrails for LLM-generated SQL. Nothing an agent writes is trusted until it passes all of:

1. static checks - a single statement, read-only keywords only, allow-listed schemas only;
2. execution in a READ ONLY transaction with a statement timeout (the database itself refuses
   writes, so a missed keyword still cannot change data);
3. evidence - the guard returns real numbers (violation rate, row count, sample rows) that are shown
   to the human reviewer next to the agent's claims."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import psycopg

from medallion.db import Database

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call|do|execute|vacuum|"
    r"analyze|cluster|reindex|lock|listen|notify|set|reset|comment|security|prepare|deallocate|refresh)\b"
    r"|pg_\w+|lo_\w+|dblink|;", re.IGNORECASE)
_SCHEMA_REF = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\.\s*[a-z_][a-z0-9_]*\b", re.IGNORECASE)
# Agents read analytics schemas only (silver, gold, ref). Operational and raw schemas are off-limits.
_DENIED_SCHEMAS = {"ops", "bronze", "public", "information_schema", "pg_catalog", "pg_temp"}
_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'")


class UnsafeSQLError(ValueError):
    pass


@dataclass(frozen=True)
class SQLEvidence:
    ok: bool
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    sample: list[dict[str, Any]] = field(default_factory=list)


def static_check(sql: str, *, kind: str) -> None:
    code = _SQL_LITERAL.sub("''", sql)  # keywords inside string literals are data, not code
    if m := _FORBIDDEN.search(code):
        raise UnsafeSQLError(f"forbidden token '{m.group(0)}'")
    if kind == "query" and not re.match(r"^\s*(select|with)\b", code, re.IGNORECASE):
        raise UnsafeSQLError("query must start with SELECT or WITH")
    if kind == "predicate" and re.search(r"\b(select|from)\b", code, re.IGNORECASE):
        raise UnsafeSQLError("a predicate must be a boolean expression over silver.tickets columns, not a query")
    if denied := {s.lower() for s in _SCHEMA_REF.findall(code)} & _DENIED_SCHEMAS:
        raise UnsafeSQLError(f"schema(s) {sorted(denied)} not allowed")


def _readonly(conn: psycopg.Connection) -> None:
    conn.execute("SET TRANSACTION READ ONLY")
    conn.execute("SET LOCAL statement_timeout = '10s'")


def evaluate_predicate(db: Database, predicate: str) -> SQLEvidence:
    """Run a violation predicate against silver.tickets. TRUE rows are violations."""
    try:
        static_check(predicate, kind="predicate")
        with db.transaction() as conn:
            _readonly(conn)
            row = conn.execute(f"SELECT count(*) AS total, count(*) FILTER (WHERE ({predicate})) AS violations "
                               f"FROM silver.tickets").fetchone()
            sample = conn.execute(f"SELECT ticket_id FROM silver.tickets WHERE ({predicate}) "
                                  f"ORDER BY ticket_id LIMIT 3").fetchall()
    except (UnsafeSQLError, psycopg.Error) as exc:
        return SQLEvidence(False, f"{type(exc).__name__}: {exc}".strip()[:500])
    rate = row["violations"] / max(1, row["total"])
    return SQLEvidence(True, metrics={"violations": row["violations"], "total": row["total"],
                                      "violation_rate": round(rate, 6)}, sample=[dict(r) for r in sample])


def evaluate_query(db: Database, sql: str) -> SQLEvidence:
    """EXPLAIN + execute a proposed model query; returns row count, columns and a small sample."""
    sql = sql.strip().rstrip(";").strip()
    try:
        static_check(sql, kind="query")
        with db.transaction() as conn:
            _readonly(conn)
            conn.execute(f"EXPLAIN {sql}")
            count = conn.execute(f"SELECT count(*) AS n FROM ({sql}) q").fetchone()["n"]
            sample = conn.execute(f"SELECT * FROM ({sql}) q LIMIT 5").fetchall()
    except (UnsafeSQLError, psycopg.Error) as exc:
        return SQLEvidence(False, f"{type(exc).__name__}: {exc}".strip()[:500])
    columns = list(sample[0].keys()) if sample else []
    return SQLEvidence(True, metrics={"rows": count, "columns": columns}, sample=[dict(r) for r in sample])
