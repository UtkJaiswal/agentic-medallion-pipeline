"""Bronze record -> silver ticket. Pure and deterministic: same bronze + same reference maps => same
silver, which is what makes the layer idempotent and re-runnable.

Outcome of every bronze row is exactly one of: silver ticket | quarantined | duplicate (audited).
The stage asserts that reconciliation on every run."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from medallion.pipeline.silver.parsers import (
    OPEN_STATUSES,
    SYSTEM_ACCOUNTS,
    PeopleResolver,
    clean_text,
    description_template,
    extract_location,
    normalize_label,
    normalize_priority,
    normalize_status,
    parse_cost,
    parse_resolution_notes,
    parse_sla_hours,
    parse_timestamp,
)
from medallion.reference import Taxonomy

_TICKET_ID = re.compile(r"^TKT-(\d+)$")


@dataclass(frozen=True)
class BronzeRecord:
    bronze_id: int
    payload: dict[str, str | None]
    row_hash: str
    source_file: str


@dataclass(frozen=True)
class LabelInfo:
    label_kind: str
    category: str
    confidence: float


@dataclass(frozen=True)
class TemplateInfo:
    category: str
    issue_type: str
    severity: str
    is_safety_hazard: bool
    confidence: float


MIN_PREFIX = 25


@dataclass
class ReferenceMaps:
    labels: dict[str, LabelInfo]
    templates: dict[str, TemplateInfo]

    def template(self, key: str) -> TemplateInfo | None:
        """Exact match, else the unique approved template this one is a truncated prefix of (the source cuts
        values at 60 chars, so a description typed into the category field arrives truncated)."""
        if (hit := self.templates.get(key)) is not None or len(key) < MIN_PREFIX:
            return hit
        matches = {t: info for t, info in self.templates.items() if t.startswith(key)}
        return next(iter(matches.values())) if len({i.category for i in matches.values()}) == 1 else None


@dataclass
class SilverTicket:
    ticket_id: str
    ticket_number: int
    created_at: datetime | None
    resolved_at: datetime | None
    resolution_hours: Decimal | None
    status: str | None
    is_open: bool | None
    priority: str | None
    category: str
    category_source: str
    category_confidence: float | None
    category_from_description: str | None
    issue_type: str | None
    inferred_severity: str | None
    is_safety_hazard: bool | None
    building: str | None
    floor: int | None
    room: str | None
    asset_id: str | None
    description: str | None
    description_template: str | None
    submitted_by: str | None
    assignee: str | None
    assignee_type: str | None
    resolution_notes: str | None
    resolution_outcome: str | None
    root_cause: str | None
    part_replaced: str | None
    eta_business_days: int | None
    onsite_hours: Decimal | None
    is_duplicate_closure: bool
    duplicate_of_ticket_id: str | None
    cost_usd: Decimal | None
    sla_hours: int | None
    sla_met: bool | None
    dq_flags: list[str]
    raw: dict[str, Any]
    bronze_id: int
    row_hash: str
    source_file: str
    content_fingerprint: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Quarantined:
    bronze_id: int
    ticket_id_raw: str | None
    reasons: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class Duplicate:
    bronze_id: int
    duplicate_ticket_id: str
    survivor_ticket_id: str
    match_rule: str


@dataclass
class SilverBuild:
    tickets: list[SilverTicket]
    quarantined: list[Quarantined]
    duplicates: list[Duplicate]


# ===================================================================================== helpers
def known_buildings(records: Iterable[BronzeRecord], junk_markers: tuple[str, ...]) -> list[str]:
    values = {clean_text(r.payload.get("building")) for r in records}
    return sorted(b for b in values if b and not any(m in b.lower() for m in junk_markers))


def quarantine_reasons(payload: dict[str, str | None], taxonomy: Taxonomy) -> list[str]:
    """Rows without a usable business key, or explicitly marked as test/junk, cannot be trusted as tickets."""
    reasons = []
    if not _TICKET_ID.match((payload.get("ticket_id") or "").strip()):
        reasons.append("invalid_ticket_id")
    text = " ".join((payload.get(c) or "") for c in ("category", "description", "building")).lower()
    if any(m in text for m in taxonomy.junk_markers):
        reasons.append("test_or_junk_marker")
    return reasons


def effective_label_and_description(payload: dict[str, str | None], maps: ReferenceMaps
                                    ) -> tuple[str, str | None, bool]:
    """Handles rows where category and description were entered in each other's fields.
    Returns (label_key, description, swapped)."""
    label_key = normalize_label(payload.get("category"))
    description = clean_text(payload.get("description"))
    info = maps.labels.get(label_key)
    desc_key = normalize_label(description)
    desc_as_label = maps.labels.get(desc_key)
    description_is_a_label = bool(desc_as_label and desc_as_label.label_kind in ("category_label", "generic_label"))
    # Swapped when the category field holds a sentence - known as one, or never seen before (new phrasing)
    # while the description field holds a known label.
    category_holds_text = (info is not None and info.label_kind == "description_text") or (
        info is None and len(label_key.split()) >= 4)
    if category_holds_text and (description is None or description_is_a_label):
        return desc_key, clean_text(payload.get("category")), True
    return label_key, description, False


def swap_candidate_labels(records: Iterable[BronzeRecord]) -> set[str]:
    """Short description values on rows whose category looks like a sentence - they may be labels."""
    out = set()
    for r in records:
        cat, desc = r.payload.get("category") or "", r.payload.get("description") or ""
        if len(cat.split()) >= 4 and 0 < len(desc.split()) <= 3:
            out.add(normalize_label(desc))
    return out


_FINGERPRINT_FIELDS = ("category", "priority", "status", "building", "description", "assigned_to",
                       "resolution_notes", "cost", "sla_hours", "resolved_at")
DEFAULT_SAFETY_BITS = 3.0  # chosen by measurement: smallest margin with 0 false merges (docs/TESTING.md)


def _values(t: SilverTicket) -> tuple[str, ...]:
    return tuple((clean_text(t.raw.get(k)) or "").lower() for k in _FINGERPRINT_FIELDS)


def _fingerprint(t: SilverTicket) -> str:
    """Every source field except the three a re-submission changes (ticket_id, created_at, submitted_by)."""
    return hashlib.sha256("\x1f".join(_values(t)).encode()).hexdigest()


def _information_bits(tickets: list[SilverTicket]) -> dict[int, float]:
    """How surprising each ticket's content is: sum over fields of -log2(share of tickets with that value).
    Two tickets can only be merged when their shared content is too rare to coincide by chance:
    with N tickets there are ~N^2/2 pairs, so a match needs >= log2(N^2) + safety bits."""
    n = max(1, len(tickets))
    rows = [_values(t) for t in tickets]
    freq = [Counter(r[i] for r in rows) for i in range(len(_FINGERPRINT_FIELDS))]
    # empty values carry no evidence that two tickets are the same, so they contribute nothing
    return {t.bronze_id: sum(-math.log2(freq[i][v] / n) for i, v in enumerate(r) if v)
            for t, r in zip(tickets, rows, strict=True)}


# ===================================================================================== transform
class SilverTransformer:
    def __init__(self, taxonomy: Taxonomy, maps: ReferenceMaps,
                 dedup_safety_bits: float = DEFAULT_SAFETY_BITS) -> None:
        self.taxonomy, self.maps = taxonomy, maps
        self.safety_bits = dedup_safety_bits

    def build(self, records: list[BronzeRecord]) -> SilverBuild:
        quarantined, candidates = [], []
        for rec in records:
            reasons = quarantine_reasons(rec.payload, self.taxonomy)
            if reasons:
                quarantined.append(Quarantined(rec.bronze_id, rec.payload.get("ticket_id"), reasons, rec.payload))
            else:
                candidates.append(rec)

        buildings = known_buildings(records, self.taxonomy.junk_markers)
        people = PeopleResolver([r.payload.get("submitted_by") for r in candidates])
        tickets = [self._ticket(r, buildings, people) for r in candidates]
        tickets, duplicates = self._deduplicate(tickets)

        ids = {t.ticket_id for t in tickets} | {d.duplicate_ticket_id for d in duplicates}
        for t in tickets:
            if t.duplicate_of_ticket_id and t.duplicate_of_ticket_id not in ids:
                t.dq_flags.append("duplicate_reference_not_found")
        return SilverBuild(tickets, quarantined, duplicates)

    def _deduplicate(self, tickets: list[SilverTicket]) -> tuple[list[SilverTicket], list[Duplicate]]:
        duplicates: list[Duplicate] = []
        # D1: same ticket_id landed more than once (e.g. corrected re-delivery) -> latest bronze row wins.
        latest: dict[str, SilverTicket] = {}
        for t in sorted(tickets, key=lambda x: x.bronze_id):
            if (prev := latest.get(t.ticket_id)) is not None:
                duplicates.append(Duplicate(prev.bronze_id, prev.ticket_id, t.ticket_id, "same_ticket_id_superseded"))
            latest[t.ticket_id] = t
        # D2: same content under a new ticket id -> the lowest ticket number (the original) survives, but only
        # when the content is informative enough that a coincidence is implausible. Otherwise keep both and
        # flag them: losing a real ticket (false merge) is worse than over-counting one (missed duplicate).
        candidates = list(latest.values())
        bits = _information_bits(candidates)
        needed = math.log2(max(2, len(candidates)) ** 2) + self.safety_bits
        groups: dict[str, list[SilverTicket]] = defaultdict(list)
        for t in candidates:
            groups[_fingerprint(t)].append(t)
        survivors: list[SilverTicket] = []
        for group in groups.values():
            group.sort(key=lambda x: x.ticket_number)
            if len(group) == 1:
                survivors.append(group[0])
            elif bits[group[0].bronze_id] < needed:
                for t in group:
                    t.dq_flags.append("possible_duplicate")
                survivors += group
            else:
                keep = group[0]
                keep.dq_flags.append("has_resubmitted_duplicates")
                survivors.append(keep)
                rule = "content_match_excl_id_created_submitter"
                duplicates += [Duplicate(d.bronze_id, d.ticket_id, keep.ticket_id, rule) for d in group[1:]]
        survivors.sort(key=lambda x: x.ticket_number)
        return survivors, duplicates

    def _ticket(self, rec: BronzeRecord, buildings: list[str], people: PeopleResolver) -> SilverTicket:
        p, flags = rec.payload, []

        def flag(f: str | None) -> None:
            if f:
                flags.append(f)

        ticket_id = (p.get("ticket_id") or "").strip()
        label_key, description, swapped = effective_label_and_description(p, self.maps)
        if swapped:
            flag("category_description_swapped")

        # --- category: the human label wins when informative; the description fills gaps and audits it
        label = self.maps.labels.get(label_key)
        template = description_template(description, buildings)
        tmpl = self.maps.template(template) if description else None
        if label is None and label_key:
            flag("category_label_unmapped")
        if description and tmpl is None:
            flag("description_template_unmapped")
        if label and label.label_kind in ("category_label", "description_text") and label.category != "unknown":
            category, source, conf = label.category, "label_map", label.confidence
        elif tmpl and tmpl.category != "unknown":
            category, source, conf = tmpl.category, "description_template", tmpl.confidence
        else:
            category, source, conf = "unknown", "unresolved", None
        desc_category = tmpl.category if tmpl and tmpl.category != "unknown" else None
        if source == "label_map" and desc_category and not self.taxonomy.are_related(category, desc_category):
            flag("category_conflicts_with_description")  # ontology overlaps (e.g. emergency lighting) are fine

        # --- dates
        created, _ = parse_timestamp(p.get("created_at"))
        resolved, _ = parse_timestamp(p.get("resolved_at"))
        if created is None:
            flag("created_at_unparseable" if clean_text(p.get("created_at")) else "created_at_missing")
        if resolved is None and clean_text(p.get("resolved_at")):
            flag("resolved_at_unparseable")
        hours = None
        if created and resolved:
            if resolved < created:
                flag("resolved_before_created")
            else:
                hours = Decimal(str(round((resolved - created).total_seconds() / 3600, 2)))

        # --- enums
        status, f = normalize_status(p.get("status"))
        flag(f)
        is_open = None if status is None else status in OPEN_STATUSES
        if is_open and resolved:
            flag("resolved_at_on_open_ticket")
        if is_open is False and resolved is None:
            flag("closed_without_resolved_at")
        priority, f = normalize_priority(p.get("priority"))
        flag(f)
        if priority is None and f is None:
            flag("priority_missing")

        # --- numbers
        cost, f = parse_cost(p.get("cost"))
        flag(f)
        sla, f = parse_sla_hours(p.get("sla_hours"))
        flag(f)
        sla_met = (hours <= sla) if hours is not None and sla is not None else None

        # --- people & parties
        submitted_by, f = people.resolve(p.get("submitted_by"))
        flag(f)
        assignee_raw = clean_text(p.get("assigned_to"))
        assignee, assignee_type = self.taxonomy.assignees.get((assignee_raw or "").lower(), (assignee_raw, None))
        if assignee_raw and assignee_type is None:
            flag("assignee_unknown")
        if category != "unknown" and self.taxonomy.is_qualified(assignee, category) is False:
            flag("assignee_not_qualified_for_category")

        # --- free text
        building = clean_text(p.get("building"))
        floor, room, asset = extract_location(description)
        notes = clean_text(p.get("resolution_notes"))
        parsed = parse_resolution_notes(notes)
        dup_of = f"TKT-{parsed['duplicate_of_number']}" if parsed["duplicate_of_number"] else None
        if dup_of and is_open:
            flag("duplicate_closure_on_open_ticket")

        ticket = SilverTicket(
            ticket_id=ticket_id, ticket_number=int(_TICKET_ID.match(ticket_id).group(1)),  # type: ignore[union-attr]
            created_at=created, resolved_at=resolved, resolution_hours=hours, status=status, is_open=is_open,
            priority=priority, category=category, category_source=source, category_confidence=conf,
            category_from_description=desc_category,
            issue_type=tmpl.issue_type if tmpl else None, inferred_severity=tmpl.severity if tmpl else None,
            is_safety_hazard=tmpl.is_safety_hazard if tmpl else None,
            building=building if building and building.lower() not in SYSTEM_ACCOUNTS else None,
            floor=floor, room=room, asset_id=asset, description=description,
            description_template=template or None, submitted_by=submitted_by, assignee=assignee,
            assignee_type=assignee_type, resolution_notes=notes, resolution_outcome=parsed["resolution_outcome"],
            root_cause=parsed["root_cause"], part_replaced=parsed["part_replaced"],
            eta_business_days=parsed["eta_business_days"], onsite_hours=parsed["onsite_hours"],
            is_duplicate_closure=dup_of is not None, duplicate_of_ticket_id=dup_of,
            cost_usd=cost, sla_hours=sla, sla_met=sla_met, dq_flags=flags, raw=p,
            bronze_id=rec.bronze_id, row_hash=rec.row_hash, source_file=rec.source_file)
        return ticket
