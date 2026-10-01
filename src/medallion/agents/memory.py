"""Agent memory.

* Semantic memory - what the system knows: approved reference maps (ref.*), taxonomy and ontology.
  Used before any LLM call: known values are never re-asked (see ClassificationAgent.ensure_mapped).
* Episodic memory - what happened: past human corrections of agent proposals. Similar episodes are
  recalled and shown to the model as precedents, so a reviewer's fix teaches the next decision.
* Procedural memory - how to do the task: short, versioned rules consolidated from review sessions
  (config/memory/procedures/<task>.md), injected into the system prompt.

Human feedback outranks model output by construction: episodes are human decisions, procedures are
human-written, and both are presented to the model as authoritative precedent."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import psycopg

from medallion.settings import PROJECT_ROOT

MEMORY_DIR = PROJECT_ROOT / "config" / "memory"
_TOKEN = re.compile(r"[a-z]+")
_STOP = frozenset({"the", "a", "an", "in", "on", "of", "and", "is", "to", "for", "at", "n", "bldg", "asset", "not"})


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1)


@dataclass(frozen=True)
class Episode:
    task: str                 # classify_labels | classify_templates
    text: str                 # the label or template that was reviewed
    agent_answer: dict        # what the agent proposed (the fields the human changed)
    human_answer: dict        # what the human decided


class AgentMemory:
    def __init__(self, procedures: dict[str, str], episodes: list[Episode], k: int = 4,
                 min_similarity: float = 0.25) -> None:
        self._procedures, self._episodes = procedures, episodes
        self._k, self._min_sim = k, min_similarity
        self._index = [(_tokens(e.text), e) for e in episodes]

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, conn: psycopg.Connection | None = None, directory: Path = MEMORY_DIR) -> AgentMemory:
        procedures = {p.stem: p.read_text().strip() for p in (directory / "procedures").glob("*.md")}
        episodes = load_episodes_from_db(conn) if conn is not None else load_episodes_from_file(
            directory / "episodes.jsonl")
        return cls(procedures, episodes)

    @property
    def fingerprint(self) -> str:
        """Part of the prompt version, so the LLM cache never serves answers made with other memory."""
        body = json.dumps({"p": self._procedures, "e": [asdict(e) for e in self._episodes]}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()[:10]

    # ------------------------------------------------------------------ recall
    def procedures_for(self, task: str) -> str:
        return self._procedures.get(task, "")

    def recall(self, task: str, texts: list[str], exclude: set[str] | None = None) -> list[Episode]:
        """Most similar past human corrections for a batch (token Jaccard). `exclude` supports
        leave-one-out evaluation: an item's own correction must never be recalled for it."""
        exclude = exclude or set()
        scored: dict[str, tuple[float, Episode]] = {}
        for text in texts:
            q = _tokens(text)
            for toks, ep in self._index:
                if ep.task != task or ep.text in exclude or not q or not toks:
                    continue
                sim = len(q & toks) / len(q | toks)
                if sim >= self._min_sim and sim > scored.get(ep.text, (0.0, ep))[0]:
                    scored[ep.text] = (sim, ep)
        return [ep for _, ep in sorted(scored.values(), key=lambda x: -x[0])[: self._k]]

    def render(self, task: str, texts: list[str], exclude: set[str] | None = None) -> tuple[str, str]:
        """(system addendum, user preamble) for a batch."""
        system = ""
        if proc := self.procedures_for(task):
            system = f"\n\n<procedures source=\"human reviewers\">\n{proc}\n</procedures>"
        episodes = self.recall(task, texts, exclude)
        user = ""
        if episodes:
            lines = [json.dumps({"input": e.text, "agent_said": e.agent_answer, "reviewer_decided": e.human_answer},
                                ensure_ascii=False) for e in episodes]
            user = ("<past_human_corrections note=\"precedents from reviewers; follow them for similar inputs\">\n"
                    + "\n".join(lines) + "\n</past_human_corrections>\n\n")
        return system, user


# ---------------------------------------------------------------------- episodic sources
_KIND_TO_TASK = {"category_label": "classify_labels", "description_template": "classify_templates"}


def load_episodes_from_db(conn: psycopg.Connection) -> list[Episode]:
    """Human corrections: the agent's original proposal vs. the reviewer's final decision."""
    rows = conn.execute(
        """SELECT p.kind, p.subject, p.proposal AS final,
                  (SELECT o.proposal FROM ops.agent_proposals o WHERE o.kind = p.kind AND o.subject = p.subject
                     AND NOT (o.proposal ? 'human_overrides') ORDER BY o.created_at LIMIT 1) AS original
           FROM ops.agent_proposals p
           WHERE p.agent = 'classification' AND p.proposal ? 'human_overrides'
             AND p.status IN ('approved', 'auto_approved')
           ORDER BY p.subject""").fetchall()
    episodes = []
    for r in rows:
        human = r["final"]["human_overrides"]
        agent = r["final"].get("agent_original") or {k: (r["original"] or {}).get(k) for k in human}
        episodes.append(Episode(_KIND_TO_TASK[r["kind"]], r["subject"], agent, human))
    return episodes


def load_episodes_from_file(path: Path) -> list[Episode]:
    if not path.exists():
        return []
    return [Episode(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def export_episodes(conn: psycopg.Connection, path: Path) -> int:
    episodes = load_episodes_from_db(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(e), ensure_ascii=False) + "\n" for e in episodes))
    return len(episodes)
