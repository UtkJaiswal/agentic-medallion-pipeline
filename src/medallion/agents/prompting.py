"""Versioned prompt files. A prompt's version is part of the LLM cache key, so editing a prompt
automatically invalidates its cached answers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    version: str
    template: Template

    def render(self, **values: str) -> str:
        return self.template.substitute(**values)


@cache
def load_prompt(name: str) -> Prompt:
    text = (PROMPTS_DIR / f"{name}.md").read_text()
    _, header, body = text.split("---", 2)
    meta = dict(line.split(":", 1) for line in header.strip().splitlines())
    return Prompt(version=meta["version"].strip(), template=Template(body.strip()))
