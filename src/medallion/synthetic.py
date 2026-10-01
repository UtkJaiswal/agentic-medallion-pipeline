"""Seeded generator of messy synthetic tickets WITH ground truth.

Every generated row carries what the pipeline *should* do with it (keep / quarantine / duplicate, the
true category, timestamp and cost), so a run on synthetic data is scored, not just smoke-tested:
`medallion synth --rows 5000 --seed 7` writes data/synthetic/tickets_<seed>.csv + a truth file.

The mess mirrors the real source (8 date formats incl. epoch, placeholders, '$' and sentinel costs,
SLA sentinels, junk rows, re-submitted and exact duplicates, swapped fields, spelling variants) and
adds drift the real file never had: new category spellings, new phrasings and new buildings."""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from medallion.settings import PROJECT_ROOT

HEADER = ["ticket_id", "created_at", "resolved_at", "category", "priority", "status", "building", "description",
          "submitted_by", "assigned_to", "resolution_notes", "cost", "sla_hours"]

BUILDINGS = ["Bengaluru Tech Park Block A", "Pune Hinjewadi Phase 2", "Hyderabad HITEC City B3",
             "Chennai OMR Annexe", "Gurugram Cyber Hub 5", "Mumbai BKC Wing C", "Noida Sector 62 Tower",
             "Kolkata Salt Lake DN-24"]
PEOPLE = [("Priya", "Sharma"), ("Rahul", "Verma"), ("Ananya", "Iyer"), ("Arjun", "Nair"), ("Sneha", "Reddy"),
          ("Vikram", "Singh"), ("Kavya", "Menon"), ("Rohan", "Gupta"), ("Meera", "Pillai"), ("Aditya", "Joshi")]
ASSIGNEES = ["maintenance team", "vendor tech", "ABC Mechanical", "overnight crew", "PestPro Services",
             "Joe (in-house)", "CityWide Electric", ""]

# (category, label spellings already in the approved label map, description patterns from approved templates)
CATEGORIES = {
    "hvac": (["HVAC", "A/C", "hvac", "Climate Control"],
             ["Temperature control issue in conference room {n}. The thermostat reads {t} degrees but it feels "
              "much colder.", "hvac broken again in {b}. its really hot"]),
    "plumbing": (["Plumbing", "PLUMBING", "water issue"],
                 ["Hot water not working in {b}. Multiple sinks affected.", "Drain clogged in janitor closet, "
                  "{f} floor."]),
    "electrical": (["Electrical", "Elec", "power issue"],
                   ["Power outlet not working at desk {n}. Tried multiple devices.",
                    "Sparking outlet in {b} — DO NOT USE, taped off"]),
    "elevator": (["Elevator", "Lift", "Vertical Transport"],
                 ["Elevator making grinding noise. {b} main elevator.", "elevator out of order again {b}"]),
    "fire_safety": (["Fire Safety", "Sprinkler", "Fire Alarm"],
                    ["Fire extinguisher expired in hallway near room {n}, {b}.", "smoke detector beeping {b}"]),
    "security_access": (["Security", "Badge/Access", "Access Control"],
                        ["Badge reader at {b} main entrance not working. Staff can't get in."]),
    "it_network": (["IT", "Network", "WiFi"],
                   ["WiFi down on {f} floor, {b}. Multiple users affected.",
                    "VPN issues from {b} conference room. Can't connect to corporate."]),
    "pest_control": (["Pest Control", "Exterminator", "Pest"],
                     ["Mice spotted in {b} kitchen area. Droppings found in cabinets.", "bugs in office {b}"]),
    "janitorial": (["Housekeeping", "Cleaning", "janitorial"],
                   ["trash overflowing in kitchen area {b}", "Spill in {b} lobby. Slip hazard."]),
    "general_maintenance": (["Maintenance", "General Maintenance"],
                            ["Paint peeling in {b} lobby. Looks bad for visitors.",
                             "Door handle broken on room {n}, {b}."]),
}
# Drift the reference data has never seen; each belongs to a real category (ground truth stays consistent).
NEW_LABELS = {"elevator": "Lift & Escalator Svc", "janitorial": "Housekeeping Svcs", "hvac": "HVAC/R"}
NEW_PHRASES = {"electrical": "Generator in {b} basement failed its weekly test",
               "pest_control": "Rats near the {b} loading bay bins"}
PRIORITY_SPELLINGS = {"low": ["low", "Low", "LOW", "lo"], "medium": ["medium", "Medium", "med", "MED", "Normal"],
                      "high": ["high", "High", "hi", "HIGH"], "critical": ["critical", "CRITICAL", "crit"]}
STATUSES = ["Open", "In Progress", "Pending Vendor", "Escalated", "Resolved", "Closed"]
PLACEHOLDERS = ["", "N/A", "NULL", "???", "unknown"]
JUNK_DESCRIPTIONS = ["DELETE ME", "asdfasdf", "IGNORE THIS ROW", "duplicate entry please delete"]


@dataclass
class Truth:
    outcome: str                     # ticket | quarantine | duplicate
    category: str | None = None
    created_at: str | None = None    # ISO, at the precision the row was rendered with
    cost_usd: float | None = None
    survivor: str | None = None      # for duplicates
    drift: bool = False              # uses a label or phrase the reference data has never seen
    notes: list[str] = field(default_factory=list)


def _render_date(dt: datetime, rng: random.Random) -> tuple[str, datetime]:
    """Render in one of the 8 source formats; return (text, value at the rendered precision)."""
    fmt = rng.choice(["iso_t", "iso_space", "iso_date", "us_12h", "us_date", "us_dash", "dd_mon", "epoch"])
    if fmt == "epoch":
        aware = dt.replace(tzinfo=UTC)
        return str(int(aware.timestamp())), dt
    if fmt == "iso_t":
        return dt.strftime("%Y-%m-%dT%H:%M:%S"), dt
    if fmt == "iso_space":
        return dt.strftime("%Y-%m-%d %H:%M:%S"), dt
    if fmt == "us_dash":
        return dt.strftime("%m-%d-%Y %H:%M:%S"), dt
    if fmt == "us_12h":
        return dt.strftime("%m/%d/%Y %I:%M %p"), dt.replace(second=0)
    if fmt == "dd_mon":
        return dt.strftime("%d-%b-%Y %H:%M"), dt.replace(second=0)
    day = dt.replace(hour=0, minute=0, second=0)
    return (dt.strftime("%Y-%m-%d") if fmt == "iso_date" else dt.strftime("%m/%d/%Y")), day


def _person(rng: random.Random) -> str:
    first, last = rng.choice(PEOPLE)
    return rng.choice([f"{first} {last}", f"{first[0]}. {last}", f"{first.lower()} {last.lower()}"])


def generate(rows: int, seed: int, out_dir: Path = PROJECT_ROOT / "data" / "synthetic",
             first_ticket: int | None = None) -> tuple[Path, Path]:
    rng = random.Random(seed)
    first_ticket = first_ticket or 1_000_000 * (seed + 1)  # disjoint id ranges per seed
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str]] = []
    truth: dict[str, Truth] = {}
    start = datetime(2025, 4, 1)

    def junk_id() -> str:
        return f"JUNK-{len(records)}"

    for i in range(rows):
        tid = f"TKT-{first_ticket + i}"
        category = rng.choice(list(CATEGORIES))
        labels, phrases = CATEGORIES[category]
        building = rng.choice(BUILDINGS)
        drift = rng.random() < 0.06 and (category in NEW_LABELS or category in NEW_PHRASES)
        label = NEW_LABELS[category] if drift and category in NEW_LABELS else rng.choice(labels)
        phrase = NEW_PHRASES[category] if drift and category in NEW_PHRASES else rng.choice(phrases)
        description = phrase.format(b=building, n=rng.randint(100, 999), f=rng.randint(1, 12), t=rng.randint(55, 80))
        created = start + timedelta(minutes=rng.randint(0, 365 * 24 * 60), seconds=rng.randint(0, 59))
        created_text, created_true = _render_date(created, rng)
        resolved = created + timedelta(hours=rng.randint(1, 240))
        status = rng.choice(STATUSES)
        resolved_text = _render_date(resolved, rng)[0] if status in ("Resolved", "Closed") else ""
        priority = rng.choice(list(PRIORITY_SPELLINGS))
        cost = round(rng.uniform(500, 15000), 2)
        options = [(f"{cost}", cost), (f"${cost:,.2f}", cost), ("-1", None), ("-999", None),
                   (rng.choice(PLACEHOLDERS), None)]
        cost_text, cost_true = rng.choices(options, weights=[60, 20, 5, 2, 13])[0]
        row = {"ticket_id": tid, "created_at": created_text, "resolved_at": resolved_text, "category": label,
               "priority": rng.choice(PRIORITY_SPELLINGS[priority]) if rng.random() > 0.1 else "",
               "status": status, "building": building, "description": description,
               "submitted_by": _person(rng), "assigned_to": rng.choice(ASSIGNEES),
               "resolution_notes": rng.choice(["fixed", "done", "Temporary fix applied. Permanent repair scheduled.",
                                               f"Parts on order. ETA {rng.randint(2, 14)} business days.", ""]),
               "cost": cost_text, "sla_hours": rng.choice(["4", "8", "24", "48", "72", "999", "0", "N/A", ""])}
        if rng.random() < 0.02:  # category and description entered in each other's fields
            row["category"], row["description"] = description, label
        records.append(row)
        truth[tid] = Truth("ticket", category, created_true.isoformat(), cost_true, drift=drift)

        if rng.random() < 0.03:  # re-submission: new id, created_at and submitter, everything else identical
            dup_id = f"TKT-{first_ticket + rows + len(records)}"
            dup = {**row, "ticket_id": dup_id, "submitted_by": _person(rng),
                   "created_at": _render_date(created + timedelta(days=rng.randint(1, 30)), rng)[0]}
            if dup["submitted_by"] == row["submitted_by"] and dup["created_at"] == row["created_at"]:
                continue
            records.append(dup)
            truth[dup_id] = Truth("duplicate", category, survivor=tid)

    for _ in range(max(3, rows // 200)):  # junk / test rows
        jid = rng.choice(["N/A", "", "NULL", junk_id()])
        records.append({**dict.fromkeys(HEADER, ""), "ticket_id": jid, "created_at": rng.choice(["asap", "TBD", ""]),
                        "description": rng.choice(JUNK_DESCRIPTIONS), "submitted_by": rng.choice(["test", "admin"])})
        truth.setdefault(f"__junk__{len(records)}", Truth("quarantine"))

    rng.shuffle(records)
    csv_path = out_dir / f"tickets_{seed}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(records)
    truth_path = out_dir / f"tickets_{seed}.truth.json"
    truth_path.write_text(json.dumps({k: v.__dict__ for k, v in truth.items()}, indent=0))
    return csv_path, truth_path


def score(db, truth_path: Path, source_file: str) -> dict:  # type: ignore[no-untyped-def]
    """Compare what the pipeline did with what it should have done."""
    truth = {k: Truth(**v) for k, v in json.loads(truth_path.read_text()).items()}
    with db.transaction() as conn:
        bronze = conn.execute("SELECT count(*) AS n FROM bronze.tickets_raw WHERE source_file = %s",
                              (source_file,)).fetchone()["n"]
        tickets = {r["ticket_id"]: r for r in conn.execute(
            """SELECT t.ticket_id, t.category, t.created_at, t.cost_usd FROM silver.tickets t
               JOIN bronze.tickets_raw b ON b.bronze_id = t._bronze_id WHERE b.source_file = %s""", (source_file,))}
        quarantined = conn.execute(
            """SELECT q.ticket_id_raw FROM silver.tickets_quarantine q JOIN bronze.tickets_raw b
               ON b.bronze_id = q._bronze_id WHERE b.source_file = %s""", (source_file,)).fetchall()
        dups = {r["duplicate_ticket_id"]: r["survivor_ticket_id"] for r in conn.execute(
            """SELECT d.* FROM silver.ticket_duplicates d JOIN bronze.tickets_raw b ON b.bronze_id = d._bronze_id
               WHERE b.source_file = %s""", (source_file,))}

    real = {k: v for k, v in truth.items() if v.outcome == "ticket"}
    true_dups = {k: v for k, v in truth.items() if v.outcome == "duplicate"}
    junk = sum(1 for v in truth.values() if v.outcome == "quarantine")
    found_true_dups = sum(1 for k, v in true_dups.items() if dups.get(k) == v.survivor)
    false_merges = [k for k in dups if k not in true_dups]
    kept = [k for k in real if k in tickets]
    created_ok = sum(1 for k in kept if tickets[k]["created_at"] is not None
                     and tickets[k]["created_at"].isoformat() == real[k].created_at)
    cost_ok = sum(1 for k in kept if (real[k].cost_usd is None and tickets[k]["cost_usd"] is None) or
                  (real[k].cost_usd is not None and tickets[k]["cost_usd"] is not None and
                   abs(float(tickets[k]["cost_usd"]) - real[k].cost_usd) < 0.01))
    cat_ok = [k for k in kept if tickets[k]["category"] == real[k].category]
    drift = [k for k in kept if real[k].drift]

    def pct(a: int, b: int) -> float | None:
        return round(100 * a / b, 2) if b else None

    return {
        "rows_in_file": bronze,
        "reconciled": bronze == len(tickets) + len(quarantined) + len(dups),
        "real_tickets_kept": f"{len(kept)}/{len(real)}",
        "quarantine": {"expected": junk, "actual": len(quarantined)},
        "duplicates": {"expected": len(true_dups), "caught": found_true_dups, "false_merges": len(false_merges),
                       "recall_pct": pct(found_true_dups, len(true_dups)),
                       "precision_pct": pct(found_true_dups, found_true_dups + len(false_merges))},
        "created_at_exact_pct": pct(created_ok, len(kept)),
        "cost_usd_exact_pct": pct(cost_ok, len(kept)),
        "category_pct": pct(len(cat_ok), len(kept)),
        "category_on_drift_rows_pct": pct(sum(1 for k in drift if k in cat_ok), len(drift)),
        "drift_rows": len(drift),
        "false_merge_examples": false_merges[:5],
    }
