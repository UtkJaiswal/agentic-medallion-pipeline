"""HTTP API: trigger pipeline runs asynchronously and poll their status; review agent proposals.

POST /v1/pipeline-runs requires an `Idempotency-Key` header. The first request with a key creates a
queued run (202 + Location); a retry with the same key and body returns that same run (200,
`Idempotent-Replayed: true`) instead of starting a second one; the same key with a different body is
rejected (422). Work happens in the background worker - clients poll GET /v1/pipeline-runs/{id}."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from medallion.agents.proposals import ProposalRepository
from medallion.container import Container
from medallion.observability import context
from medallion.resilience.rate_limit import TokenBucket
from medallion.settings import PROJECT_ROOT

log = logging.getLogger("medallion.api")
DATA_ROOT = (PROJECT_ROOT / "data").resolve()


class RunRequest(BaseModel):
    source_path: str | None = Field(None, description="CSV under ./data to ingest (default: RAW_DATA_PATH)")
    as_of: date | None = Field(None, description="snapshot date for backlog marts")


class Decision(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)


class ClientRateLimiter:
    """Per-client token buckets, LRU-bounded so a flood of distinct clients can't exhaust memory.
    In-process: fine for one API replica; use Redis for several."""

    def __init__(self, rpm: int, max_clients: int = 10_000) -> None:
        self._rpm, self._max = rpm, max_clients
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> float:
        with self._lock:
            bucket = self._buckets.pop(client, None) or TokenBucket.per_minute(self._rpm)
            self._buckets[client] = bucket
            if len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
        return bucket.try_acquire()


def create_app(container: Container | None = None) -> FastAPI:
    holder: dict[str, Container] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        holder["c"] = container or Container()
        yield
        if container is None:  # close only what this app created
            holder["c"].close()

    app = FastAPI(title="Medallion pipeline API", version="1.0.0", lifespan=lifespan)

    def c() -> Container:
        return holder["c"]

    # ----------------------------------------------------------------------------- middleware
    @app.middleware("http")
    async def trace_and_log(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        trace_id = context.trace_id_from_headers(dict(request.headers))
        started = time.perf_counter()
        with context.bind(trace_id=trace_id):
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            log.info("http.request", extra={"method": request.method, "path": request.url.path,
                                            "status": response.status_code,
                                            "ms": round((time.perf_counter() - started) * 1000, 1)})
        return response

    limiter_holder: dict[str, ClientRateLimiter] = {}

    def authorize(request: Request, x_api_key: str | None = Header(None)) -> None:
        expected = c().settings.api_key
        if expected is not None and not secrets.compare_digest(x_api_key or "", expected.get_secret_value()):
            raise HTTPException(401, "missing or invalid X-API-Key")

    def rate_limited(request: Request) -> None:
        limiter = limiter_holder.setdefault("l", ClientRateLimiter(c().settings.api_rate_limit_rpm))
        client = request.headers.get("x-api-key") or (request.client.host if request.client else "anonymous")
        if (wait := limiter.retry_after(client)) > 0:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(max(1, round(wait)))})

    # ----------------------------------------------------------------------------- health
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        try:
            with c().db.transaction() as conn:
                conn.execute("SELECT 1")
        except psycopg.Error as exc:
            raise HTTPException(503, f"database unavailable: {exc}") from exc
        return {"status": "ready"}

    # ----------------------------------------------------------------------------- pipeline runs
    @app.post("/v1/pipeline-runs", status_code=202, dependencies=[Depends(authorize), Depends(rate_limited)])
    def create_run(body: RunRequest, response: Response,
                   idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128)
                   ) -> dict[str, Any]:
        params = body.model_dump(mode="json", exclude_none=True)
        if body.source_path:
            path = (PROJECT_ROOT / body.source_path).resolve()
            if not path.is_relative_to(DATA_ROOT) or not path.is_file():
                raise HTTPException(422, "source_path must be an existing file under ./data")
            params["source_path"] = str(path)
        request_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        trace_id = context.current_trace_id()
        with c().db.transaction() as conn:
            row = conn.execute(
                """INSERT INTO ops.pipeline_runs (run_id, trace_id, status, trigger, params, idempotency_key,
                                                  request_hash, max_attempts)
                   VALUES (%s, %s, 'queued', 'api', %s, %s, %s, %s)
                   ON CONFLICT (idempotency_key) DO NOTHING RETURNING run_id""",
                (str(uuid.uuid4()), trace_id, json.dumps(params), idempotency_key, request_hash,
                 c().settings.job_max_attempts)).fetchone()
            if row is None:  # key seen before: replay, never re-execute
                existing = conn.execute("SELECT * FROM ops.pipeline_runs WHERE idempotency_key = %s",
                                        (idempotency_key,)).fetchone()
                if existing["request_hash"] != request_hash:
                    raise HTTPException(422, "Idempotency-Key was already used with a different request body")
                response.status_code = 200
                response.headers["Idempotent-Replayed"] = "true"
                run_id = existing["run_id"]
            else:
                run_id = row["run_id"]
                c().events.publish(conn, "pipeline.run.queued", str(run_id), {"trigger": "api", **params})
        response.headers["Location"] = f"/v1/pipeline-runs/{run_id}"
        return _run_view(c(), str(run_id))

    @app.get("/v1/pipeline-runs/{run_id}", dependencies=[Depends(authorize)])
    def get_run(run_id: uuid.UUID, response: Response) -> dict[str, Any]:
        view = _run_view(c(), str(run_id))
        if view["status"] in ("queued", "running"):
            response.headers["Retry-After"] = "2"  # polling hint for clients
        return view

    @app.get("/v1/pipeline-runs", dependencies=[Depends(authorize)])
    def list_runs(limit: int = 20) -> list[dict[str, Any]]:
        with c().db.transaction() as conn:
            return conn.execute("""SELECT run_id, status, trigger, attempts, created_at, finished_at, error
                                   FROM ops.pipeline_runs ORDER BY created_at DESC LIMIT %s""",
                                (min(max(limit, 1), 100),)).fetchall()

    # ----------------------------------------------------------------------------- HITL review
    @app.get("/v1/proposals", dependencies=[Depends(authorize)])
    def list_proposals(status: str = "proposed", agent: str | None = None) -> list[dict[str, Any]]:
        with c().db.transaction() as conn:
            return ProposalRepository(c().events).list(conn, status=status, agent=agent)

    @app.post("/v1/proposals/{proposal_id}/{action}", dependencies=[Depends(authorize), Depends(rate_limited)])
    def decide(proposal_id: uuid.UUID, action: Literal["approve", "reject"], body: Decision) -> dict[str, Any]:
        try:
            with c().db.transaction() as conn:
                row = ProposalRepository(c().events).decide(conn, str(proposal_id), approve=action == "approve",
                                                            reviewer=body.reviewer)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"proposal_id": str(proposal_id), "status": row["status"]}

    @app.exception_handler(psycopg.OperationalError)
    async def db_down(_: Request, exc: psycopg.OperationalError) -> JSONResponse:
        return JSONResponse({"detail": "database unavailable"}, status_code=503, headers={"Retry-After": "5"})

    return app


def _run_view(container: Container, run_id: str) -> dict[str, Any]:
    with container.db.transaction() as conn:
        run = conn.execute("""SELECT run_id, trace_id, status, trigger, params, attempts, max_attempts, error, summary,
                                     created_at, started_at, finished_at
                              FROM ops.pipeline_runs WHERE run_id = %s""", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "run not found")
        stages = conn.execute("""SELECT stage, status, rows_in, rows_out, rows_rejected, alerts, started_at,
                                        finished_at FROM ops.stage_runs WHERE run_id = %s ORDER BY id""",
                              (run_id,)).fetchall()
    return {**run, "stages": stages}


def asgi_app() -> FastAPI:  # uvicorn factory entry point
    return create_app()

