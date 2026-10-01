"""Outbox relay: publishes committed events from ops.outbox to Kafka (only when Kafka is enabled).

Delivery is at-least-once: an event is marked published only after the broker acknowledged it, so a
crash between ack and commit re-sends it. Every message carries its `event_id` header so consumers can
de-duplicate; the producer itself is idempotent (no duplicates from its own internal retries)."""

from __future__ import annotations

import json
import logging
import random
import threading
from typing import Any, Protocol

from medallion.db import Database
from medallion.resilience.retry import RetryPolicy

log = logging.getLogger(__name__)


class MessageProducer(Protocol):
    def send(self, topic: str, key: str, value: bytes, headers: dict[str, str]) -> None: ...
    def flush(self, timeout_s: float) -> list[str]:
        """Wait for outstanding sends; return the event ids that failed delivery (empty = all acked)."""
        ...


class KafkaProducer:
    """confluent-kafka adapter (optional dependency: `pip install .[kafka]`)."""

    def __init__(self, bootstrap_servers: str) -> None:
        from confluent_kafka import Producer

        self._producer = Producer({"bootstrap.servers": bootstrap_servers, "enable.idempotence": True,
                                   "acks": "all", "linger.ms": 20, "delivery.timeout.ms": 30000})
        self._failed: list[str] = []

    def send(self, topic: str, key: str, value: bytes, headers: dict[str, str]) -> None:
        def on_delivery(err: Any, msg: Any) -> None:
            if err is not None:
                self._failed.append(dict(msg.headers() or [])["event_id"].decode())
        self._producer.produce(topic, key=key, value=value, headers=list(headers.items()), on_delivery=on_delivery)
        self._producer.poll(0)

    def flush(self, timeout_s: float) -> list[str]:
        remaining = self._producer.flush(timeout_s)
        failed, self._failed = self._failed, []
        if remaining:
            raise TimeoutError(f"{remaining} messages not acknowledged within {timeout_s}s")
        return failed


class OutboxRelay:
    def __init__(self, db: Database, producer: MessageProducer, topic: str, batch_size: int = 200) -> None:
        self._db, self._producer, self._topic, self._batch = db, producer, topic, batch_size
        self.stop = threading.Event()

    def relay_once(self) -> int:
        with self._db.transaction() as conn:
            rows = conn.execute(
                """SELECT event_id, event_type, aggregate_id, payload, trace_id, created_at FROM ops.outbox
                   WHERE published_at IS NULL ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s""",
                (self._batch,)).fetchall()
            if not rows:
                return 0
            for r in rows:
                body = {"event_id": str(r["event_id"]), "event_type": r["event_type"],
                        "aggregate_id": r["aggregate_id"], "occurred_at": r["created_at"].isoformat(),
                        "trace_id": r["trace_id"], "payload": r["payload"]}
                self._producer.send(self._topic, r["aggregate_id"], json.dumps(body).encode(),
                                    {"event_id": str(r["event_id"]), "event_type": r["event_type"],
                                     "trace_id": r["trace_id"] or ""})
            failed = set(self._producer.flush(30))
            delivered = [str(r["event_id"]) for r in rows if str(r["event_id"]) not in failed]
            conn.execute("UPDATE ops.outbox SET published_at = now() WHERE event_id = ANY(%s::uuid[])", (delivered,))
        if failed:
            log.warning("relay.partial_failure", extra={"failed": len(failed)})
        return len(delivered)

    def run_forever(self, poll_interval_s: float) -> None:
        policy, failures = RetryPolicy(base_delay_s=1, max_delay_s=60), 0
        log.info("relay.started", extra={"topic": self._topic})
        while not self.stop.is_set():
            try:
                sent = self.relay_once()
                failures = 0
                if sent:
                    log.info("relay.published", extra={"events": sent})
                    continue
                self.stop.wait(poll_interval_s * (0.5 + random.random()))
            except Exception as exc:  # broker or DB unavailable: back off with jitter and keep going
                failures += 1
                delay = policy.backoff(failures)
                log.warning("relay.error", extra={"error": str(exc)[:300], "retry_in_s": round(delay, 1)})
                self.stop.wait(delay)
