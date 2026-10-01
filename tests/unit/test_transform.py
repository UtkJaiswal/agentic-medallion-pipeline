import pytest

from medallion.pipeline.silver.transform import (
    BronzeRecord,
    LabelInfo,
    ReferenceMaps,
    SilverTransformer,
    TemplateInfo,
)

BASE = {"ticket_id": "TKT-1000", "created_at": "2024-01-01 08:00:00", "resolved_at": "2024-01-01 12:00:00",
        "category": "Plumbing", "priority": "high", "status": "Resolved", "building": "Tower A",
        "description": "Faucet in Tower A kitchen won't turn off completely. Constant drip.",
        "submitted_by": "John Smith", "assigned_to": "ABC Mechanical", "resolution_notes": "fixed",
        "cost": "$120.50", "sla_hours": "8"}
FAUCET = "faucet in <bldg> kitchen won't turn off completely. constant drip."


@pytest.fixture
def maps():
    return ReferenceMaps(
        labels={"plumbing": LabelInfo("category_label", "plumbing", 0.95),
                "pest": LabelInfo("category_label", "pest_control", 0.95),
                "other": LabelInfo("generic_label", "unknown", 0.95),
                "bugs in office tower a": LabelInfo("description_text", "pest_control", 0.9),
                "hvac": LabelInfo("category_label", "hvac", 0.95)},
        templates={FAUCET: TemplateInfo("plumbing", "dripping_faucet", "low", False, 0.9),
                   "bugs in office <bldg>": TemplateInfo("pest_control", "insects", "medium", False, 0.9)})


def rec(i: int, **overrides) -> BronzeRecord:
    payload = {**BASE, "ticket_id": f"TKT-{1000 + i}", **overrides}
    return BronzeRecord(bronze_id=i, payload=payload, row_hash=f"h{i}", source_file="t.csv")


def build(taxonomy, maps, records):
    return SilverTransformer(taxonomy, maps).build(records)


def test_happy_path_types_and_derivations(taxonomy, maps):
    out = build(taxonomy, maps, [rec(1)])
    t = out.tickets[0]
    assert (t.category, t.category_source, t.status, t.is_open, t.priority) == \
        ("plumbing", "label_map", "resolved", False, "high")
    assert str(t.resolution_hours) == "4.0" and t.sla_met is True
    assert str(t.cost_usd) == "120.50"
    assert (t.assignee, t.assignee_type) == ("ABC Mechanical", "vendor")
    assert t.issue_type == "dripping_faucet" and t.dq_flags == []


def test_every_bronze_row_is_accounted_for(taxonomy, maps):
    records = [rec(1), rec(2, ticket_id="N/A"), rec(3, description="DELETE ME"), rec(4)]
    out = build(taxonomy, maps, records)
    assert len(out.tickets) + len(out.quarantined) + len(out.duplicates) == len(records)
    assert {q.bronze_id for q in out.quarantined} == {2, 3}


def _population(n=200):
    """Varied tickets, so that a shared value is genuinely informative."""
    return [rec(i + 10, description=f"Faucet {i} leaking in kitchen", cost=f"{100 + i}.25",
                resolved_at=f"2024-02-{1 + i % 28:02d} {i % 24:02d}:00:00", building=f"Block {i % 17}",
                priority=["low", "high", "crit", "med"][i % 4], assigned_to=["vendor tech", "maintenance team"][i % 2],
                sla_hours=["4", "8", "24", "48"][i % 4], status=["Open", "Resolved", "Closed"][i % 3])
            for i in range(n)]


def test_resubmitted_ticket_is_deduplicated_to_lowest_number(taxonomy, maps):
    population = _population()
    original = population[5]
    resubmitted = BronzeRecord(999, {**original.payload, "ticket_id": "TKT-11050", "created_at": "2025-01-01",
                                     "submitted_by": "J. Doe"}, "h999", "t.csv")
    out = build(taxonomy, maps, [*population, resubmitted])
    assert [(d.duplicate_ticket_id, d.survivor_ticket_id) for d in out.duplicates] == \
        [("TKT-11050", original.payload["ticket_id"])]


def test_low_information_twins_are_flagged_not_merged(taxonomy, maps):
    """Identical but ordinary tickets could be a coincidence: keep both (a false merge loses a ticket)."""
    common = {"description": "bugs in office", "cost": "", "resolved_at": "", "resolution_notes": "fixed",
              "priority": "", "status": "Open", "assigned_to": "", "sla_hours": "", "building": "Tower A"}
    out = build(taxonomy, maps, [*_population(), rec(1, **common), rec(2, **common)])
    twins = [t for t in out.tickets if t.ticket_id in ("TKT-1001", "TKT-1002")]
    assert len(twins) == 2 and not out.duplicates
    assert all("possible_duplicate" in t.dq_flags for t in twins)


def test_sparse_lookalikes_are_not_merged(taxonomy, maps):
    sparse = {"resolved_at": "", "cost": "", "resolution_notes": "", "assigned_to": "", "sla_hours": "",
              "priority": ""}
    out = build(taxonomy, maps, [rec(1, **sparse), rec(2, **sparse)])
    assert len(out.tickets) == 2 and not out.duplicates


def test_same_ticket_id_latest_bronze_row_wins(taxonomy, maps):
    out = build(taxonomy, maps, [rec(1, status="Open"), BronzeRecord(9, {**BASE, "ticket_id": "TKT-1001"}, "h9", "b")])
    assert len(out.tickets) == 1 and out.tickets[0].status == "resolved"
    assert out.duplicates[0].match_rule == "same_ticket_id_superseded"


def test_swapped_category_and_description(taxonomy, maps):
    out = build(taxonomy, maps, [rec(1, category="bugs in office Tower A", description="Pest")])
    t = out.tickets[0]
    assert "category_description_swapped" in t.dq_flags
    assert t.description == "bugs in office Tower A"
    assert t.category == "pest_control"


def test_generic_label_falls_back_to_description(taxonomy, maps):
    t = build(taxonomy, maps, [rec(1, category="Other")]).tickets[0]
    assert (t.category, t.category_source) == ("plumbing", "description_template")


def test_label_wins_but_unrelated_conflict_is_flagged(taxonomy, maps):
    t = build(taxonomy, maps, [rec(1, category="Pest")]).tickets[0]  # pest label, plumbing description
    assert t.category == "pest_control" and t.category_from_description == "plumbing"
    assert "category_conflicts_with_description" in t.dq_flags


def test_ontology_related_categories_are_not_conflicts(taxonomy, maps):
    t = build(taxonomy, maps, [rec(1, category="HVAC")]).tickets[0]  # hvac ~ plumbing (condensate leaks)
    assert taxonomy.are_related("hvac", "plumbing")
    assert t.category == "hvac" and "category_conflicts_with_description" not in t.dq_flags


def test_assignee_outside_ontology_qualification_is_flagged(taxonomy, maps):
    pest_on_plumbing = build(taxonomy, maps, [rec(1, assigned_to="PestPro Services")]).tickets[0]
    in_house = build(taxonomy, maps, [rec(1, assigned_to="maintenance team")]).tickets[0]
    assert "assignee_not_qualified_for_category" in pest_on_plumbing.dq_flags
    assert "assignee_not_qualified_for_category" not in in_house.dq_flags  # generalists: no claim made


def test_resolved_before_created_is_flagged_not_negative(taxonomy, maps):
    t = build(taxonomy, maps, [rec(1, resolved_at="2023-12-01")]).tickets[0]
    assert t.resolution_hours is None and t.sla_met is None
    assert "resolved_before_created" in t.dq_flags


def test_unmapped_values_are_flagged_and_unresolved(taxonomy, maps):
    t = build(taxonomy, maps, [rec(1, category="Roof", description="Roof leaking in lobby")]).tickets[0]
    assert t.category == "unknown" and t.category_source == "unresolved"
    assert {"category_label_unmapped", "description_template_unmapped"} <= set(t.dq_flags)


def test_duplicate_closure_detected(taxonomy, maps):
    out = build(taxonomy, maps, [rec(1), rec(2, resolution_notes="Duplicate of ticket #1001. Closing.",
                                                description="Something else entirely here", cost="1")])
    dup = next(t for t in out.tickets if t.ticket_id == "TKT-1002")
    assert dup.is_duplicate_closure and dup.duplicate_of_ticket_id == "TKT-1001"
    assert "duplicate_reference_not_found" not in dup.dq_flags


def test_transform_is_deterministic(taxonomy, maps):
    records = [rec(i) for i in range(1, 6)] + [rec(9, ticket_id="TKT-11001", created_at="2025-01-01")]
    a, b = build(taxonomy, maps, records), build(taxonomy, maps, list(reversed(records)))
    assert [t.ticket_id for t in a.tickets] == [t.ticket_id for t in b.tickets]
    assert [(d.duplicate_ticket_id, d.survivor_ticket_id) for d in a.duplicates] == \
        [(d.duplicate_ticket_id, d.survivor_ticket_id) for d in b.duplicates]


def test_truncated_template_inherits_the_approved_mapping(maps):
    truncated = FAUCET[:50]
    assert maps.template(truncated).category == "plumbing"
    assert maps.template("faucet") is None  # too short to be trusted as a prefix


def test_swap_detected_even_when_the_sentence_was_never_seen(taxonomy, maps):
    t = build(taxonomy, maps, [rec(1, category="Faucet in Tower A kitchen won't turn off completely. Constant drip.",
                                   description="Plumbing")]).tickets[0]
    assert "category_description_swapped" in t.dq_flags
    assert t.category == "plumbing" and t.description.startswith("Faucet")
