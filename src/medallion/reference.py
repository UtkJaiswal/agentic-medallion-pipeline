"""Reference data: the business taxonomy (config/taxonomy.yaml) and the approved, version-controlled
seed mappings (config/seeds/*.csv). Seeds are agent output that a human reviewed and committed - see
docs/AGENTS.md. Loading is an idempotent upsert that never overwrites a later approval."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import psycopg
import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Taxonomy:
    categories: dict[str, str]           # category -> description
    keywords: dict[str, list[str]]
    generic_labels: frozenset[str]
    assignees: dict[str, tuple[str, str]]  # normalised raw -> (display name, in_house|vendor)
    junk_markers: tuple[str, ...]
    related: frozenset[frozenset[str]] = frozenset()    # ontology: categories that legitimately overlap
    qualified_for: dict[str, frozenset[str]] = field(default_factory=dict)  # party -> categories

    def are_related(self, a: str, b: str) -> bool:
        return a == b or frozenset((a, b)) in self.related

    def is_qualified(self, party: str | None, category: str) -> bool | None:
        """None when the ontology says nothing about the party (e.g. in-house generalists)."""
        allowed = self.qualified_for.get(party or "")
        return None if allowed is None else category in allowed

    @classmethod
    def load(cls, path: Path) -> Taxonomy:
        doc = yaml.safe_load(path.read_text())
        cats = doc["categories"]
        onto_path = path.with_name("ontology.yaml")
        onto = yaml.safe_load(onto_path.read_text()) if onto_path.exists() else {}
        return cls(
            categories={k: v["description"] for k, v in cats.items()},
            keywords={k: [w.lower() for w in v.get("keywords", [])] for k, v in cats.items()},
            generic_labels=frozenset(doc.get("generic_labels", [])),
            assignees={k: (v["name"], v["type"]) for k, v in doc.get("assignees", {}).items()},
            junk_markers=tuple(m.lower() for m in doc.get("junk_markers", [])),
            related=frozenset(frozenset(pair) for pair in onto.get("related", [])),
            qualified_for={k: frozenset(v) for k, v in onto.get("qualified_for", {}).items()},
        )

    @cached_property
    def names(self) -> tuple[str, ...]:
        return tuple(self.categories)

    def prompt_block(self) -> str:
        return "\n".join(f"- {k}: {v}" for k, v in self.categories.items())


def load_reference_data(conn: psycopg.Connection, taxonomy: Taxonomy, seeds_dir: Path) -> dict[str, int]:
    for cat, desc in taxonomy.categories.items():
        conn.execute("""INSERT INTO ref.category_taxonomy (category, description) VALUES (%s, %s)
                        ON CONFLICT (category) DO UPDATE SET description = EXCLUDED.description""", (cat, desc))
    counts = {"taxonomy": len(taxonomy.categories)}
    conn.execute("DELETE FROM ref.category_relations")
    for pair in taxonomy.related:
        a, b = sorted(pair)
        conn.execute("INSERT INTO ref.category_relations (category_a, category_b) VALUES (%s, %s)", (a, b))
    conn.execute("DELETE FROM ref.party_qualifications")
    for party, cats in taxonomy.qualified_for.items():
        for cat in sorted(cats):
            conn.execute("INSERT INTO ref.party_qualifications (party, category) VALUES (%s, %s)", (party, cat))

    # Seeds never overwrite a mapping approved later (by a human or the agent): DO NOTHING.
    labels = _read_csv(seeds_dir / "category_label_map.csv")
    for r in labels:
        conn.execute(
            """INSERT INTO ref.category_label_map (label, label_kind, category, confidence, source, approved_by)
               VALUES (%s, %s, %s, %s, %s, 'seed') ON CONFLICT (label) DO NOTHING""",
            (r["label"], r["label_kind"], r["category"], r["confidence"], r["source"]))
    templates = _read_csv(seeds_dir / "description_template_map.csv")
    for r in templates:
        conn.execute(
            """INSERT INTO ref.description_template_map (template, category, issue_type, severity,
                       is_safety_hazard, confidence, source, approved_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'seed') ON CONFLICT (template) DO NOTHING""",
            (r["template"], r["category"], r["issue_type"], r["severity"], r["is_safety_hazard"] == "true",
             r["confidence"], r["source"]))
    checks = _read_yaml_list(seeds_dir / "dq_checks.yaml")
    for c in checks:
        conn.execute(
            """INSERT INTO ref.dq_checks (check_id, description, rationale, severity, violation_predicate,
                                          threshold, source, approved_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'seed') ON CONFLICT (check_id) DO NOTHING""",
            (c["check_id"], c["description"], c["rationale"], c["severity"], c["violation_predicate"],
             c["threshold"], c.get("source", "seed")))
    counts |= {"label_seeds": len(labels), "template_seeds": len(templates), "dq_check_seeds": len(checks)}
    return counts


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_yaml_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text()) or []
