"""Metadata auto-tagging at landing: semantic type, sensitivity and PII hints per bronze column.

Deterministic on purpose - column names plus value-shape evidence from the profile are enough here,
and a governance tag must be reproducible. Tags are hints for downstream access policies (e.g. mask
`pii` columns in BI tools), so false positives are preferred over misses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PERSON = re.compile(r"^[A-Z][a-z]*\.? ?[A-Za-z]+(?: [A-Za-z]+)?$|^[a-z]+ [a-z]+$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_NUMERIC_TYPES = {"identifier", "timestamp", "monetary", "duration"}


@dataclass(frozen=True)
class ColumnTag:
    column: str
    semantic_type: str
    sensitivity: str  # public | internal | pii
    tags: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _share(values: list[dict[str, Any]], pattern: re.Pattern[str]) -> float:
    total = sum(v["n"] for v in values if v["value"])
    hit = sum(v["n"] for v in values if v["value"] and pattern.search(v["value"]))
    return hit / total if total else 0.0


def tag_column(column: str, profile: dict[str, Any]) -> ColumnTag:
    name = column.lower()
    top = profile.get("top_values", [])
    shapes = [s["shape"] for s in profile.get("shapes", [])]
    evidence: dict[str, Any] = {"distinct": profile.get("distinct_raw")}
    tags: list[str] = []

    if name.endswith("_id") or name == "id":
        semantic, sensitivity = "identifier", "internal"
    elif name.endswith("_at") or any(re.search(r"9{4}-99-99|99/99/9999", s) for s in shapes[:3]):
        semantic, sensitivity = "timestamp", "internal"
    elif any(k in name for k in ("cost", "amount", "price")):
        semantic, sensitivity = "monetary", "internal"
        tags.append("finance")
    elif name.endswith("_hours") or "duration" in name:
        semantic, sensitivity = "duration", "internal"
    elif (person_share := _share(top, _PERSON)) >= 0.5 and any(k in name for k in ("by", "name", "user", "owner")):
        semantic, sensitivity = "person_name", "pii"
        tags.append("pii:name")
        evidence["person_name_share_top_values"] = round(person_share, 2)
    elif (profile.get("avg_length") or 0) > 25 or profile.get("distinct_raw", 0) > 500:
        semantic, sensitivity = "free_text", "internal"
        tags.append("may_contain_pii")  # humans type names, phone numbers, etc. into free text
    else:
        semantic, sensitivity = "categorical", "public"

    # digits-and-separators looks like a phone number, but so does "2024-03-15 10:30": only check text columns
    phone_like = semantic not in _NUMERIC_TYPES and _share(top, _PHONE) > 0.1
    if _share(top, _EMAIL) > 0 or phone_like:
        sensitivity = "pii"
        tags.append("pii:contact")
    if name == "assigned_to":
        tags.append("org:vendor_or_team")
    if profile.get("placeholder", 0) > 0:
        tags.append("has_placeholder_values")
    return ColumnTag(column, semantic, sensitivity, tags, evidence)
