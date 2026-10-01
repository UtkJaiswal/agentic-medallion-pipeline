from datetime import datetime
from decimal import Decimal

import pytest

from medallion.pipeline.silver.parsers import (
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


@pytest.mark.parametrize(("raw", "expected", "fmt"), [
    ("2024-10-24 14:32:10", datetime(2024, 10, 24, 14, 32, 10), "iso_space"),
    ("2024-07-25T18:27:44", datetime(2024, 7, 25, 18, 27, 44), "iso_t"),
    ("2025-07-01", datetime(2025, 7, 1), "iso_date"),
    ("02/14/2025 08:16 AM", datetime(2025, 2, 14, 8, 16), "us_12h"),
    ("02/03/2025", datetime(2025, 2, 3), "us_date"),           # MM/DD, never DD/MM (see profile evidence)
    ("06-04-2025 22:51:35", datetime(2025, 6, 4, 22, 51, 35), "us_dash_24h"),
    ("12-Feb-2025 03:21", datetime(2025, 2, 12, 3, 21), "dd_mon_yyyy"),
    ("1726547768", datetime(2024, 9, 17, 4, 36, 8), "epoch"),   # epoch seconds are UTC
])
def test_parse_timestamp_formats(raw, expected, fmt):
    assert parse_timestamp(raw) == (expected, fmt)


@pytest.mark.parametrize("raw", ["", None, "asap", "TBD", "not a date", "00/00/0000", "???", "never", "13/45/2024",
                                 "0000000001"])
def test_parse_timestamp_rejects_junk(raw):
    assert parse_timestamp(raw) == (None, None)


@pytest.mark.parametrize(("raw", "expected", "flag"), [
    ("4569.86", Decimal("4569.86"), None),
    ("$1,403.56", Decimal("1403.56"), None),
    ("0", Decimal("0.00"), None),
    ("-1", None, "cost_negative_sentinel"),
    ("-999", None, "cost_negative_sentinel"),
    ("N/A", None, None),
    ("TBD", None, None),
    ("error", None, None),
    ("abc", None, "cost_unparseable"),
    ("5000000", None, "cost_implausible"),
])
def test_parse_cost(raw, expected, flag):
    assert parse_cost(raw) == (expected, flag)


@pytest.mark.parametrize(("raw", "expected", "flag"), [
    ("24", 24, None), ("4", 4, None), ("999", None, "sla_sentinel"), ("0", None, "sla_out_of_range"),
    ("-1", None, "sla_out_of_range"), ("N/A", None, None), ("NULL", None, None), ("x", None, "sla_unparseable"),
])
def test_parse_sla_hours(raw, expected, flag):
    assert parse_sla_hours(raw) == (expected, flag)


@pytest.mark.parametrize(("raw", "expected"), [
    ("hi", "high"), ("HIGH", "high"), ("crit", "critical"), ("CRITICAL", "critical"), ("med", "medium"),
    ("Normal", "medium"), ("lo", "low"), ("urgent!!!", "critical"), ("", None), ("???", None),
])
def test_normalize_priority(raw, expected):
    assert normalize_priority(raw)[0] == expected


def test_normalize_status():
    assert normalize_status("Pending Vendor") == ("pending_vendor", None)
    assert normalize_status("In Progress") == ("in_progress", None)
    assert normalize_status("NULL") == (None, None)
    assert normalize_status("weird") == (None, "status_unrecognised")


def test_clean_text_and_label():
    assert clean_text("  Tower   B ") == "Tower B"
    assert clean_text("unknown") is None
    assert normalize_label("  Pest   Control ") == "pest control"


def test_description_template_masks_variable_parts():
    buildings = ["HQ Floor 2", "Tower B", "Building 7"]
    a = description_template("Breaker keeps tripping in server room 391. Critical — affects production.", buildings)
    b = description_template("Breaker keeps tripping in server room 738. Critical — affects production.", buildings)
    assert a == b == "breaker keeps tripping in server room <n>. critical — affects production."
    assert description_template("Elevator H-011 stuck between floors 1 and 9.", buildings) == \
        "elevator <asset> stuck between floors <n> and <n>."
    assert description_template("bugs in office HQ Floor 2", buildings) == "bugs in office <bldg>"


def test_extract_location():
    assert extract_location("WiFi down on 4 floor, Building 7. Multiple users affected.") == (4, None, None)
    assert extract_location("Power outlet not working at desk 392.") == (None, "desk 392", None)
    assert extract_location("Elevator H-011 stuck between floors 1 and 9.") == (None, None, "H-011")
    assert extract_location("complaints from staff on 7th floor") == (7, None, None)


@pytest.mark.parametrize(("note", "outcome", "field", "value"), [
    ("Duplicate of ticket #3098. Closing.", "duplicate", "duplicate_of_number", 3098),
    ("Parts on order. ETA 9 business days.", "parts_on_order", "eta_business_days", 9),
    ("Completed. Took 7.6 hours on site.", "completed", "onsite_hours", Decimal("7.6")),
    ("Dispatched technician. Fixed on site. Replaced fan belt.", "fixed_on_site", "part_replaced", "fan belt"),
    ("Resolved. Root cause: power surge.", "resolved_root_cause", "root_cause", "power surge"),
    ("fixed", "resolved", "root_cause", None),
    ("something novel", "other", "root_cause", None),
])
def test_parse_resolution_notes(note, outcome, field, value):
    parsed = parse_resolution_notes(note)
    assert parsed["resolution_outcome"] == outcome
    assert parsed[field] == value


def test_people_resolver_merges_variants_and_nicknames():
    names = ["John Smith", "john smith", "J. Smith", "Robert Martinez", "robert martinez", "Bob Martinez",
             "R. Martinez", "Tom Wilson", "T. Wilson", "test", ""]
    r = PeopleResolver(names)
    assert r.resolve("J. Smith") == ("John Smith", None)
    assert r.resolve("john smith") == ("John Smith", None)
    assert r.resolve("Bob Martinez") == ("Robert Martinez", None)
    assert r.resolve("R. Martinez") == ("Robert Martinez", None)
    assert r.resolve("T. Wilson") == ("Tom Wilson", None)
    assert r.resolve("admin") == (None, "submitter_system_account")
    assert r.resolve("") == (None, None)


def test_people_resolver_refuses_to_guess_ambiguous_initials():
    r = PeopleResolver(["John Smith", "Jane Smith", "J. Smith"])
    name, flag = r.resolve("J. Smith")
    assert flag == "submitter_ambiguous"
    assert name == "J. Smith"
