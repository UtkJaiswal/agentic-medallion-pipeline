"""Gold: business models built with plain SQL files (sql/gold/*.sql, applied in order).

All files run in ONE transaction - Postgres DDL is transactional, so consumers switch from the old
gold to the new gold atomically and a failure leaves the previous gold untouched."""

from __future__ import annotations

from pathlib import Path

from medallion.pipeline.stage import RunContext, Stage, StageResult

GOLD_SQL_DIR = Path(__file__).resolve().parents[3] / "sql" / "gold"


class GoldStage(Stage):
    name = "gold"

    def __init__(self, sql_dir: Path = GOLD_SQL_DIR) -> None:
        self._sql_dir = sql_dir

    def execute(self, ctx: RunContext) -> StageResult:
        files = sorted(self._sql_dir.glob("*.sql"))
        with ctx.db.transaction() as conn:
            as_of = ctx.as_of or conn.execute("SELECT max(created_at)::date AS d FROM silver.tickets").fetchone()["d"]
            conn.execute("SELECT set_config('medallion.run_id', %s, true), set_config('medallion.as_of', %s, true)",
                         (ctx.run_id, str(as_of)))
            for f in files:
                conn.execute(f.read_text())
            tables = conn.execute("""SELECT c.relname AS name, c.reltuples::bigint AS approx FROM pg_class c
                                     JOIN pg_namespace n ON n.oid = c.relnamespace
                                     WHERE n.nspname = 'gold' AND c.relkind = 'r' ORDER BY 1""").fetchall()
            counts = {t["name"]: conn.execute(f"SELECT count(*) AS n FROM gold.{t['name']}").fetchone()["n"]
                      for t in tables}
            silver_rows = conn.execute("SELECT count(*) AS n FROM silver.tickets").fetchone()["n"]
        alerts = []
        if counts.get("fct_tickets") != silver_rows:
            alerts.append(f"fct_tickets has {counts.get('fct_tickets')} rows but silver has {silver_rows}")
        return StageResult(rows_in=silver_rows, rows_out=counts.get("fct_tickets"),
                           metrics={"as_of": str(as_of), "tables": counts, "sql_files": [f.name for f in files]},
                           alerts=alerts)
