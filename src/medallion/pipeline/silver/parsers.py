"""Value-level cleaning rules. Pure functions: no I/O, trivially unit-testable.

Each rule that changes or discards a value reports a flag; flags land in silver.tickets.dq_flags so
every correction stays auditable. Rule rationale is documented in docs/CLEANING_RULES.md."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

# Placeholder tokens observed in the source that mean "no value" (matched case-insensitively).
NULL_TOKENS = frozenset({"", "null", "none", "n/a", "na", "???", "unknown", "tbd", "error", "-", ".",
                         "not a date", "asap", "never", "pending", "00/00/0000"})


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    return None if collapsed.lower() in NULL_TOKENS else collapsed


# ---------------------------------------------------------------------------------------- dates
# Order matters only for readability: the formats are mutually exclusive. Slash/dash day-month order
# is MM/DD - in this source the first component is never > 12 while the second often is (see the
# profile in docs/CLEANING_RULES.md), which rules out DD/MM.
_DATE_FORMATS = (
    ("iso_t", "%Y-%m-%dT%H:%M:%S"),
    ("iso_space", "%Y-%m-%d %H:%M:%S"),
    ("iso_date", "%Y-%m-%d"),
    ("us_12h", "%m/%d/%Y %I:%M %p"),
    ("us_date", "%m/%d/%Y"),
    ("us_dash_24h", "%m-%d-%Y %H:%M:%S"),
    ("dd_mon_yyyy", "%d-%b-%Y %H:%M"),
)
_MIN_YEAR, _MAX_YEAR = 2000, 2100


def parse_timestamp(value: str | None) -> tuple[datetime | None, str | None]:
    """Returns (timestamp, format_name). Epoch seconds are converted from UTC; other formats carry no
    timezone and are kept as wall-clock time."""
    text = clean_text(value)
    if text is None:
        return None, None
    if re.fullmatch(r"\d{10}", text):
        ts = datetime.fromtimestamp(int(text), UTC).replace(tzinfo=None)
        return (ts, "epoch") if _MIN_YEAR <= ts.year <= _MAX_YEAR else (None, None)
    for name, fmt in _DATE_FORMATS:
        try:
            ts = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (ts, name) if _MIN_YEAR <= ts.year <= _MAX_YEAR else (None, None)
    return None, None


# ---------------------------------------------------------------------------------------- numbers
MAX_PLAUSIBLE_COST = Decimal("1000000")


def parse_cost(value: str | None) -> tuple[Decimal | None, str | None]:
    """'$1,403.56' -> 1403.56. Negative values (-1, -999) are sentinels, not refunds."""
    text = clean_text(value)
    if text is None:
        return None, None
    try:
        amount = Decimal(text.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None, "cost_unparseable"
    if amount < 0:
        return None, "cost_negative_sentinel"
    if amount > MAX_PLAUSIBLE_COST:
        return None, "cost_implausible"
    return amount.quantize(Decimal("0.01")), None


SLA_SENTINELS = frozenset({999, 9999})


def parse_sla_hours(value: str | None) -> tuple[int | None, str | None]:
    text = clean_text(value)
    if text is None:
        return None, None
    try:
        hours = int(float(text))
    except ValueError:
        return None, "sla_unparseable"
    if hours in SLA_SENTINELS:
        return None, "sla_sentinel"
    if not 1 <= hours <= 24 * 30:
        return None, "sla_out_of_range"
    return hours, None


# ---------------------------------------------------------------------------------------- enums
_PRIORITY = {
    "low": "low", "lo": "low",
    "medium": "medium", "med": "medium", "normal": "medium",
    "high": "high", "hi": "high",
    "critical": "critical", "crit": "critical", "urgent": "critical", "urgent!!!": "critical",
}
_STATUS = {
    "open": "open", "in progress": "in_progress", "pending vendor": "pending_vendor",
    "escalated": "escalated", "resolved": "resolved", "closed": "closed",
}
OPEN_STATUSES = frozenset({"open", "in_progress", "pending_vendor", "escalated"})


def normalize_priority(value: str | None) -> tuple[str | None, str | None]:
    text = clean_text(value)
    if text is None:
        return None, None
    mapped = _PRIORITY.get(text.lower())
    return (mapped, None) if mapped else (None, "priority_unrecognised")


def normalize_status(value: str | None) -> tuple[str | None, str | None]:
    text = clean_text(value)
    if text is None:
        return None, None
    mapped = _STATUS.get(text.lower())
    return (mapped, None) if mapped else (None, "status_unrecognised")


# ---------------------------------------------------------------------------------------- free text
def normalize_label(value: str | None) -> str:
    """Key used to look up a raw category label in ref.category_label_map ('' for empty)."""
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


_ASSET = re.compile(r"\b[A-Z]-\d{3}\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def description_template(text: str | None, buildings: list[str]) -> str:
    """Mask the variable parts of a description so semantically identical tickets share one key.
    'Breaker keeps tripping in server room 391.' -> 'breaker keeps tripping in server room <n>.'
    This is what makes LLM enrichment cheap: ~4.6k distinct descriptions collapse to ~100 templates."""
    if not text:
        return ""
    out = text
    for b in sorted(buildings, key=len, reverse=True):
        out = re.sub(re.escape(b), "<bldg>", out, flags=re.IGNORECASE)
    out = _ASSET.sub("<asset>", out)
    out = _NUMBER.sub("<n>", out)
    return re.sub(r"\s+", " ", out).strip().lower()


_FLOOR = re.compile(r"\b(\d{1,3})(?:st|nd|rd|th)?\s+floor\b|\bfloor\s+(\d{1,3})\b(?!\s+and)", re.IGNORECASE)
_ROOM = re.compile(r"\b(room|conf room|conference room|desk|cubicle|door|spot)\s+(\d{1,4})\b", re.IGNORECASE)


def extract_location(text: str | None) -> tuple[int | None, str | None, str | None]:
    """(floor, room, asset_id) mentioned in a description, if any."""
    if not text:
        return None, None, None
    floor = room = None
    if m := _FLOOR.search(text):
        floor = int(m.group(1) or m.group(2))
    if m := _ROOM.search(text):
        room = f"{m.group(1).lower()} {m.group(2)}"
    asset = m.group(0) if (m := _ASSET.search(text)) else None
    return floor, room, asset


# Resolution notes are templated by the source system; regex extraction is exact and free.
_NOTE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^duplicate of ticket #(?P<dup>\d+)", re.I), "duplicate"),
    (re.compile(r"^temporary fix applied", re.I), "temporary_fix"),
    (re.compile(r"^issue could not be reproduced", re.I), "not_reproduced"),
    (re.compile(r"^parts on order\. eta (?P<eta>\d+) business days", re.I), "parts_on_order"),
    (re.compile(r"^vendor called\. scheduled for (?P<day>\w+)", re.I), "vendor_scheduled"),
    (re.compile(r"^completed\. took (?P<hours>[\d.]+) hours on site", re.I), "completed"),
    (re.compile(r"^dispatched technician\. fixed on site\. replaced (?P<part>[\w ]+?)\.?$", re.I), "fixed_on_site"),
    (re.compile(r"^resolved\. root cause: (?P<cause>[\w ]+?)\.?$", re.I), "resolved_root_cause"),
    (re.compile(r"^resolved by (?P<by>.+?)\.?$", re.I), "resolved"),
    (re.compile(r"^(fixed|done)\.?$", re.I), "resolved"),
]


def parse_resolution_notes(text: str | None) -> dict[str, object]:
    out: dict[str, object] = {"resolution_outcome": None, "root_cause": None, "part_replaced": None,
                              "eta_business_days": None, "onsite_hours": None, "duplicate_of_number": None}
    if not text:
        return out
    for pattern, outcome in _NOTE_PATTERNS:
        if m := pattern.match(text):
            g = m.groupdict()
            out["resolution_outcome"] = outcome
            out["root_cause"] = g.get("cause")
            out["part_replaced"] = g.get("part")
            out["eta_business_days"] = int(g["eta"]) if g.get("eta") else None
            out["onsite_hours"] = Decimal(g["hours"]) if g.get("hours") else None
            out["duplicate_of_number"] = int(g["dup"]) if g.get("dup") else None
            return out
    out["resolution_outcome"] = "other"
    return out


# ---------------------------------------------------------------------------------------- people
SYSTEM_ACCOUNTS = frozenset({"test", "admin", "system", "root", "user"})
_NICKNAMES = {"bob": "robert", "rob": "robert", "mike": "michael", "tom": "thomas", "bill": "william",
              "jim": "james", "kate": "katherine", "liz": "elizabeth", "dave": "david", "chris": "christopher"}


class PeopleResolver:
    """Resolves 'J. Smith', 'john smith', 'John Smith' to one canonical person.

    Identity key = (first initial after nickname expansion, surname). The display name is the most
    frequent full-form spelling. If two different full first names share a key ('John Smith' and
    'Jane Smith'), initials for that key are ambiguous and are left unresolved rather than guessed."""

    def __init__(self, names: list[str | None]) -> None:
        full_forms: dict[tuple[str, str], Counter[str]] = {}
        first_names: dict[tuple[str, str], set[str]] = {}
        for raw in names:
            parsed = self._parse(raw)
            if parsed is None:
                continue
            key, first, display = parsed
            if len(first) > 1:
                full_forms.setdefault(key, Counter())[display] += 1
                first_names.setdefault(key, set()).add(_NICKNAMES.get(first, first))
        self._display = {k: c.most_common(1)[0][0] for k, c in full_forms.items()}
        self._ambiguous = {k for k, firsts in first_names.items() if len(firsts) > 1}

    @staticmethod
    def _parse(raw: str | None) -> tuple[tuple[str, str], str, str] | None:
        text = clean_text(raw)
        if text is None or text.lower() in SYSTEM_ACCOUNTS:
            return None
        parts = text.replace(".", ". ").split()
        if len(parts) < 2:
            return None
        first, last = parts[0].rstrip(".").lower(), parts[-1].lower()
        initial = _NICKNAMES.get(first, first)[0]
        return (initial, last), first, " ".join(p.capitalize() for p in text.split())

    def resolve(self, raw: str | None) -> tuple[str | None, str | None]:
        text = clean_text(raw)
        if text is None:
            return None, None
        if text.lower() in SYSTEM_ACCOUNTS:
            return None, "submitter_system_account"
        parsed = self._parse(raw)
        if parsed is None:
            return text, "submitter_unparsed"
        key, first, display = parsed
        if len(first) > 1 and key not in self._ambiguous:
            return self._display.get(key, display), None
        if key in self._display and key not in self._ambiguous:
            return self._display[key], None
        return display, "submitter_ambiguous"
