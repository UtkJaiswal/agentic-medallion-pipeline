"""Composition root: the one place that wires concrete implementations together (dependency injection
by hand - no framework). Entry points (CLI, API, worker, relay) ask the container for collaborators."""

from __future__ import annotations

from functools import cached_property

from medallion.db import Database
from medallion.events.outbox import OutboxPublisher
from medallion.llm.factory import build_router
from medallion.llm.router import LLMRouter
from medallion.observability import logging as obs_logging
from medallion.pipeline.runner import PipelineRunner
from medallion.reference import Taxonomy
from medallion.settings import Settings, get_settings


class Container:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        obs_logging.configure(self.settings.log_level, self.settings.log_format, self.settings.timezone)

    @cached_property
    def db(self) -> Database:
        return Database(self.settings.database_url, max_size=max(8, self.settings.llm_concurrency + 4)).open()

    @cached_property
    def events(self) -> OutboxPublisher:
        return OutboxPublisher()

    @cached_property
    def taxonomy(self) -> Taxonomy:
        return Taxonomy.load(self.settings.taxonomy_path)

    def router(self) -> LLMRouter:
        """A fresh router per run: budgets are per run, circuit breakers start closed."""
        return build_router(self.settings, self.db)

    @cached_property
    def runner(self) -> PipelineRunner:
        return PipelineRunner(self.settings, self.db, self.events, self.router)

    def close(self) -> None:
        if "db" in self.__dict__:
            self.db.close()
