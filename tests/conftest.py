import os
import uuid

import psycopg
import pytest

from medallion.reference import Taxonomy
from medallion.settings import PROJECT_ROOT, Settings


@pytest.fixture(scope="session")
def taxonomy() -> Taxonomy:
    return Taxonomy.load(PROJECT_ROOT / "config" / "taxonomy.yaml")


# ------------------------------------------------------------------------------------- integration
def _admin_url() -> str:
    return os.getenv("DATABASE_URL", Settings().database_url)


@pytest.fixture(scope="session")
def test_database_url():
    """A throw-away database per test session on the same server; skipped if Postgres isn't running."""
    admin = _admin_url()
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            name = f"medallion_test_{uuid.uuid4().hex[:8]}"
            conn.execute(f"CREATE DATABASE {name}")
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({exc.__class__.__name__}); run `make up` for integration tests")
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def container(test_database_url, monkeypatch):
    from medallion.container import Container
    from medallion.db import migrate

    settings = Settings(database_url=test_database_url, llm_providers=[], log_format="text", api_rate_limit_rpm=100_000)
    c = Container(settings)
    migrate(c.db)
    with c.db.transaction() as conn:  # every test starts from empty layers
        conn.execute("""TRUNCATE bronze.tickets_raw, silver.tickets, silver.tickets_quarantine,
                        silver.ticket_duplicates, ops.stage_runs, ops.pipeline_runs, ops.ingestion_manifest,
                        ops.outbox, ops.agent_proposals, ops.dq_results, ops.llm_calls, ops.llm_cache,
                        ops.column_profiles, ref.category_label_map, ref.description_template_map, ref.dq_checks
                        CASCADE""")
    yield c
    c.close()
