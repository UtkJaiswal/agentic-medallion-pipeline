"""Domain events via the transactional outbox pattern.

Producers call `publish(conn, ...)` inside the same transaction as the state change, so an event
exists if and only if the change committed. Delivery to Kafka is a separate concern handled by the
relay (events/relay.py); without Kafka the outbox is still a queryable audit/lineage log."""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

import psycopg

from medallion.observability.context import current_trace_id


class EventPublisher(Protocol):
    def publish(self, conn: psycopg.Connection, event_type: str, aggregate_id: str,
                payload: dict[str, Any]) -> str: ...


class OutboxPublisher:
    def publish(self, conn: psycopg.Connection, event_type: str, aggregate_id: str,
                payload: dict[str, Any]) -> str:
        event_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO ops.outbox (event_id, event_type, aggregate_id, payload, trace_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (event_id, event_type, aggregate_id, json.dumps(payload, default=str), current_trace_id()))
        return event_id
