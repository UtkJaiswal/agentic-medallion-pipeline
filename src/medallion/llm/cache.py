"""LLM response cache (Repository pattern: in-memory for tests, Postgres for real runs)."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Protocol

from medallion.db import Database
from medallion.llm.types import LLMRequest, LLMResponse


class LLMCache(Protocol):
    def get(self, key: str) -> LLMResponse | None: ...
    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None: ...


class InMemoryLLMCache:
    def __init__(self) -> None:
        self._data: dict[str, LLMResponse] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> LLMResponse | None:
        with self._lock:
            hit = self._data.get(key)
        return replace(hit, cached=True) if hit else None

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        with self._lock:
            self._data[key] = response


class PostgresLLMCache:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> LLMResponse | None:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT * FROM ops.llm_cache WHERE cache_key = %s", (key,)).fetchone()
        if row is None:
            return None
        return LLMResponse(text=row["response_text"], provider=row["provider"], model=row["model"],
                           input_tokens=row["input_tokens"], output_tokens=row["output_tokens"], cached=True)

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """INSERT INTO ops.llm_cache (cache_key, provider, model, task, prompt_version, response_text,
                                              input_tokens, output_tokens)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (cache_key) DO NOTHING""",
                (key, response.provider, response.model, request.task, request.prompt_version,
                 response.text, response.input_tokens, response.output_tokens))
