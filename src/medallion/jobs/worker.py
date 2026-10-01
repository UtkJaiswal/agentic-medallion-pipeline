"""Background worker: executes queued pipeline runs.

* Claiming uses `FOR UPDATE SKIP LOCKED`, so any number of workers can poll the same queue safely.
* A claimed run holds a lease that a heartbeat thread keeps extending; if a worker dies, the lease
  expires and another worker re-claims the run (the pipeline is idempotent, so re-execution is safe).
* Transient failures are re-queued with exponential backoff + jitter (`not_before`) up to
  `max_attempts`; permanent failures (e.g. a blocking quality gate) are not retried.
* Idle polling sleeps a jittered interval so a fleet of workers doesn't hit the database in lockstep."""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
from typing import Any

from medallion.container import Container
from medallion.observability import context
from medallion.resilience.retry import PermanentError, RetryPolicy, TransientError

log = logging.getLogger(__name__)

_CLAIM = """
UPDATE ops.pipeline_runs SET status = 'running', attempts = attempts + 1, worker_id = %(worker)s,
       lease_expires_at = now() + make_interval(secs => %(lease)s), started_at = coalesce(started_at, now())
WHERE run_id = (
    SELECT run_id FROM ops.pipeline_runs
    WHERE (status = 'queued' AND not_before <= now())
       OR (status = 'running' AND lease_expires_at < now())          -- crashed worker: reclaim
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1)
RETURNING run_id, trace_id, attempts, max_attempts"""


class Worker:
    def __init__(self, container: Container, worker_id: str | None = None) -> None:
        self.c = container
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.stop = threading.Event()
        s = container.settings
        self._retry = RetryPolicy(base_delay_s=10, max_delay_s=300)
        self._poll_s, self._lease_s = s.worker_poll_interval_s, s.job_lease_s

    def claim(self) -> dict[str, Any] | None:
        with self.c.db.transaction() as conn:
            return conn.execute(_CLAIM, {"worker": self.worker_id, "lease": self._lease_s}).fetchone()

    def run_forever(self) -> None:
        log.info("worker.started", extra={"worker_id": self.worker_id})
        while not self.stop.is_set():
            job = self.claim()
            if job is None:
                self.stop.wait(self._poll_s * (0.5 + random.random()))  # jittered idle poll
                continue
            self.process(job)
        log.info("worker.stopped", extra={"worker_id": self.worker_id})

    def process(self, job: dict[str, Any]) -> None:
        run_id = str(job["run_id"])
        with context.bind(trace_id=job["trace_id"], run_id=run_id):
            log.info("worker.job_claimed", extra={"attempt": job["attempts"], "max_attempts": job["max_attempts"]})
            done = threading.Event()
            beat = threading.Thread(target=self._heartbeat, args=(run_id, done), daemon=True)
            beat.start()
            try:
                self.c.runner.execute(run_id)
            except PermanentError as exc:
                log.error("worker.job_failed_permanently", extra={"error": str(exc)[:300]})
            except Exception as exc:
                self._retry_or_fail(job, exc)
            finally:
                done.set()
                beat.join(timeout=5)

    def _retry_or_fail(self, job: dict[str, Any], exc: Exception) -> None:
        # transient by type, or unknown infrastructure errors; programming errors are not worth retrying
        retryable = isinstance(exc, TransientError) or not isinstance(exc, ValueError | TypeError | KeyError)
        if retryable and job["attempts"] < job["max_attempts"]:
            delay = self._retry.backoff(job["attempts"])
            with self.c.db.transaction() as conn:
                conn.execute("""UPDATE ops.pipeline_runs
                                SET status = 'queued', worker_id = NULL, lease_expires_at = NULL, finished_at = NULL,
                                    not_before = now() + make_interval(secs => %s), error = %s
                                WHERE run_id = %s""",
                             (delay, f"attempt {job['attempts']}: {exc}"[:2000], job["run_id"]))
            log.warning("worker.job_requeued", extra={"retry_in_s": round(delay, 1), "error": str(exc)[:300]})
        else:
            with self.c.db.transaction() as conn:
                conn.execute("""UPDATE ops.pipeline_runs SET status = 'failed', lease_expires_at = NULL,
                                       finished_at = now(), error = %s WHERE run_id = %s""",
                             (f"{type(exc).__name__}: {exc}"[:2000], job["run_id"]))
            log.error("worker.job_failed", extra={"attempts": job["attempts"], "error": str(exc)[:300]})

    def _heartbeat(self, run_id: str, done: threading.Event) -> None:
        while not done.wait(self._lease_s / 3):
            with self.c.db.transaction() as conn:
                conn.execute("""UPDATE ops.pipeline_runs SET lease_expires_at = now() + make_interval(secs => %s)
                                WHERE run_id = %s AND worker_id = %s""", (self._lease_s, run_id, self.worker_id))


def main(container: Container) -> None:
    worker = Worker(container)
    for sig in (signal.SIGTERM, signal.SIGINT):  # finish the current job, then exit
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.run_forever()
