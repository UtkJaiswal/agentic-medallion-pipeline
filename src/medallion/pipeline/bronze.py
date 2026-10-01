"""Bronze: land the source file as-is. Schema-on-read, append-only, no data loss.

* Every field is stored verbatim (as text) in a jsonb payload, so new/removed/renamed columns never
  break ingestion - they are detected and reported as schema drift instead.
* Lineage per row: source file + its sha256, row number, ingested_at, run_id, and a row_hash
  (sha256 of the canonical payload) for duplicate tracking across files.
* Idempotent: the file is content-addressed. Re-landing identical bytes inserts nothing; the unique
  key (file_sha256, row_number) makes even a crashed half-run safe to repeat. Rows and the manifest
  entry are written in one transaction."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from pathlib import Path

from medallion.agents.profiler import profile_table
from medallion.pipeline.stage import RunContext, Stage, StageResult
from medallion.pipeline.tagging import tag_column

log = logging.getLogger(__name__)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_hash(payload: dict[str, str | None]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str | None]]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, restkey="_extra_fields")
        header = list(reader.fieldnames or [])
        rows = []
        for row in reader:
            # a short row yields None for missing fields: keep them as JSON null rather than inventing ''
            rows.append({k: (v if not isinstance(v, list) else json.dumps(v)) for k, v in row.items()})
    return header, rows


class BronzeIngestStage(Stage):
    name = "bronze"

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def execute(self, ctx: RunContext) -> StageResult:
        path = (self._path or ctx.settings.raw_data_path).resolve()
        source = ctx.settings.source_name
        sha = file_sha256(path)
        header, rows = read_rows(path)
        fingerprint = hashlib.sha256(json.dumps(header).encode()).hexdigest()[:16]
        alerts: list[str] = []

        with ctx.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM ops.ingestion_manifest WHERE source_name = %s AND file_sha256 = %s",
                            (source, sha)).fetchone():
                log.info("bronze.already_ingested", extra={"file": path.name, "sha256": sha[:12]})
                return StageResult(rows_in=len(rows), rows_out=0, rows_rejected=0,
                                   metrics={"file": path.name, "sha256": sha, "skipped": "already ingested"})

            previous = conn.execute("""SELECT header FROM ops.ingestion_manifest WHERE source_name = %s
                                       ORDER BY ingested_at DESC LIMIT 1""", (source,)).fetchone()
            drift = None
            if previous and previous["header"] != header:
                drift = {"added": [c for c in header if c not in previous["header"]],
                         "removed": [c for c in previous["header"] if c not in header]}
                alerts.append(f"schema drift in {path.name}: {drift}")
                ctx.events.publish(conn, "bronze.schema_drift", source, {"file": path.name, **drift})

            conn.execute("CREATE TEMP TABLE _landing (LIKE bronze.tickets_raw INCLUDING DEFAULTS) ON COMMIT DROP")
            with conn.cursor().copy("""COPY _landing (source_name, source_file, source_file_sha256, source_row_number,
                                                       payload, row_hash, run_id) FROM STDIN""") as copy:
                for i, payload in enumerate(rows, start=1):
                    copy.write_row((source, path.name, sha, i, json.dumps(payload, ensure_ascii=False),
                                    row_hash(payload), ctx.run_id))
            inserted = conn.execute("""
                INSERT INTO bronze.tickets_raw (source_name, source_file, source_file_sha256, source_row_number,
                                                payload, row_hash, ingested_at, run_id)
                SELECT source_name, source_file, source_file_sha256, source_row_number, payload, row_hash, now(), run_id
                FROM _landing ON CONFLICT (source_file_sha256, source_row_number) DO NOTHING""").rowcount
            conn.execute("""INSERT INTO ops.ingestion_manifest (source_name, file_sha256, source_path, header,
                                header_fingerprint, row_count, schema_drift, run_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                         (source, sha, str(path), json.dumps(header), fingerprint, len(rows),
                          json.dumps(drift) if drift else None, ctx.run_id))
            repeated = conn.execute("""SELECT count(*) AS n FROM bronze.tickets_raw t
                                       WHERE t.source_file_sha256 = %s AND EXISTS (
                                         SELECT 1 FROM bronze.tickets_raw o WHERE o.row_hash = t.row_hash
                                         AND o.source_file_sha256 <> t.source_file_sha256)""", (sha,)).fetchone()["n"]

        tags = self._profile_and_tag(ctx, header)
        return StageResult(
            rows_in=len(rows), rows_out=inserted, rows_rejected=0,
            metrics={"file": path.name, "sha256": sha, "header_fingerprint": fingerprint,
                     "rows_seen_in_earlier_files": repeated, "schema_drift": drift,
                     "pii_columns": [t.column for t in tags if t.sensitivity == "pii"]},
            alerts=alerts)

    def _profile_and_tag(self, ctx: RunContext, header: list[str]) -> list:
        with ctx.db.transaction() as conn:
            profile = profile_table(conn, header)
            tags = [tag_column(col, p) for col, p in profile["columns"].items()]
            for col, p in profile["columns"].items():
                conn.execute("""INSERT INTO ops.column_profiles (run_id, layer, table_name, column_name, profile)
                                VALUES (%s, 'bronze', 'tickets_raw', %s, %s)""",
                             (ctx.run_id, col, json.dumps(p, default=str)))
            for t in tags:
                conn.execute(
                    """INSERT INTO ops.column_catalog (source_name, column_name, semantic_type, tags, sensitivity,
                                                       evidence, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, now())
                       ON CONFLICT (source_name, column_name) DO UPDATE SET semantic_type = EXCLUDED.semantic_type,
                           tags = EXCLUDED.tags, sensitivity = EXCLUDED.sensitivity, evidence = EXCLUDED.evidence,
                           updated_at = now()""",
                    (ctx.settings.source_name, t.column, t.semantic_type, t.tags, t.sensitivity,
                     json.dumps(t.evidence, default=str)))
        return tags
