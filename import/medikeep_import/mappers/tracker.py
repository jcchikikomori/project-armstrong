"""health_tracker.csv -- the Google Forms response sheet.

688 rows, 31 columns, one discriminator column and branching sections, so a
row is either a BP reading or a dose or a symptom or a sleep log, never all of
them. Everything in this module keys off that discriminator.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..models import MappingResult, Record, Warning
from ..normalize import clean, join_notes, parse_datetime, parse_int, squish, tag
from ..sources import Row

DISCRIMINATOR = "Anong nararamdaman mo ngayon?"

BP = "I'm logging my BP results"
MEDICATION = "I'm logging my medication"
SYMPTOM = "May naramdaman ako"
FOOD = "I'm logging my food/drink intake"
SLEEP = "Gusto ko i-record tulog ko"
EMS = 'Parang "mamamatay" na ako (EMS)'
FITNESS = "I'm logging my activity (Fitness)"

TIMESTAMP = "Timestamp"
SYSTOLIC = "SYSTOLIC mm Hg (top/upper number)"
DIASTOLIC = "DIASTOLIC mm Hg (bottom/lower number)"
SPO2 = "Blood Oxygen (%SpO2 in percentage)"
HEART_RATE = "Heart rate"
PERIOD = "What period?"
BP_FEELING = "How are you feeling"  # no question mark; column 15
SYMPTOM_FEELING = "How are you feeling?"  # question mark; column 3
REMARKS = "I-explain mo (Additional Remarks)"

# app/schemas/vitals.py rejects anything outside these with a 422, and a 422
# kills the whole request. Checking here means one bad field is dropped
# instead of one good row being lost.
RANGES = {
    "systolic_bp": (60, 250),
    "diastolic_bp": (30, 150),
    "heart_rate": (30, 250),
    "oxygen_saturation": (70, 100),
}

# Vitals has no tags column, so this is the marker instead. It also unlocks
# DELETE /vitals/patient/{id}/import/{import_source}/date/{date} as an undo.
IMPORT_SOURCE = "gsheets-health-tracker"

# The Forms checkbox options, in full. The tokenizer needs them verbatim
# because two of them contain commas, which is exactly why the cell cannot be
# split on commas.
SYMPTOM_OPTIONS = (
    'Aches (mostly Headache, otherwise use "Other")',
    "Anxiety Trigger (Anxious)",
    "Arms Shaking",
    "Chest Pains",
    "Dizzy/Nahihilo",
)

# The source records no severity at all. Occurrences require one, so this is
# the floor, chosen so nothing reads as worse than it was reported.
DEFAULT_SEVERITY = "mild"
EMS_SEVERITY = "critical"

# Free-text complaints, grouped so the symptom list stays readable: 84 distinct
# one-off descriptions would bury Dizzy/Nahihilo (47) and Anxiety (37). The
# text itself survives verbatim in the occurrence notes. --split-freetext
# turns each one into its own symptom instead.
FREETEXT_SYMPTOM = "Other (self-reported)"

_BACKDATE = re.compile(r"^(?P<period>[^(]+?)\s*\((?P<date>\d{4}-\d{2}-\d{2})\)$")


def tokenize_feelings(cell: str) -> tuple[list[str], str | None]:
    """Split a multi-select cell into known options and a free-text tail.

    Longest-match from the left, because the option labels themselves contain
    commas: splitting `Aches (mostly Headache, otherwise use "Other")` on `,`
    invents a symptom called `otherwise use "Other")`. Once no option matches
    at the cursor, whatever is left is the Forms "Other" free text -- one
    value, even if it contains commas of its own.
    """
    rest = (cell or "").strip()
    known: list[str] = []
    options = sorted(SYMPTOM_OPTIONS, key=len, reverse=True)
    while rest:
        match = next((o for o in options if rest == o or rest.startswith(o + ",")), None)
        if match is None:
            break
        known.append(match)
        rest = rest[len(match) :].lstrip(", ").strip()
    return known, rest or None


def _vitals_field(row: Row, column: str, field: str, result: MappingResult, key: str):
    value = parse_int(row.get(column))
    if value is None:
        return None
    low, high = RANGES[field]
    if not low <= value <= high:
        # No clamping. A 119% SpO2 clamped to 100 reads as a real measurement;
        # a dropped field reads as what it is, a missing one.
        result.warnings.append(
            Warning(key, "out-of-range", f"{field}={value} outside {low}-{high}, field dropped")
        )
        return None
    return value


def _recorded_at(row: Row, key: str, result: MappingResult) -> tuple[datetime | None, str | None]:
    """Resolve the reading's timestamp, honouring backdates in `What period?`.

    Two rows carry `Evening (2026-05-12)` -- a backdate typed into an enum.
    The embedded date wins; the submit time is kept as a note so the edit is
    visible rather than silently applied.
    """
    submitted = parse_datetime(row.get(TIMESTAMP))
    period = squish(row.get(PERIOD))
    if not period:
        return submitted, None
    match = _BACKDATE.match(period)
    if not match or submitted is None:
        return submitted, period
    backdated = datetime.strptime(match.group("date"), "%Y-%m-%d").replace(
        hour=submitted.hour, minute=submitted.minute, second=submitted.second
    )
    result.warnings.append(
        Warning(key, "backdated", f"{period!r} -> recorded_date {backdated.date()}")
    )
    note = f"{match.group('period').strip()} (submitted {submitted.isoformat(sep=' ')})"
    return backdated, note


def map_vitals(rows: list[Row], patient_id: int) -> MappingResult:
    result = MappingResult()
    for row in rows:
        if row.get(DISCRIMINATOR) != BP:
            continue
        key = row.key("vitals")
        recorded_at, period_note = _recorded_at(row, key, result)
        if recorded_at is None:
            result.warnings.append(Warning(key, "skipped", "no parseable Timestamp"))
            continue

        payload = {
            "patient_id": patient_id,
            # The column is a datetime, not a date, so a date-only value would
            # be rejected.
            "recorded_date": recorded_at.isoformat(),
            "import_source": IMPORT_SOURCE,
        }
        for column, field in (
            (SYSTOLIC, "systolic_bp"),
            (DIASTOLIC, "diastolic_bp"),
            (HEART_RATE, "heart_rate"),
            (SPO2, "oxygen_saturation"),
        ):
            value = _vitals_field(row, column, field, result, key)
            if value is not None:
                payload[field] = value

        notes = join_notes(clean(row.get(BP_FEELING)), period_note, clean(row.get(REMARKS)))
        if notes:
            payload["notes"] = notes
        result.records.append(Record(key, "vitals", "/vitals/", payload))
    return result


def map_symptoms(rows: list[Row], patient_id: int, *, split_freetext: bool = False) -> MappingResult:
    """Symptom parents plus one occurrence per row.

    A parent is shared by every row that reports it, so parents are emitted
    first and occurrences point back by parent_key.
    """
    result = MappingResult()
    relevant = [r for r in rows if r.get(DISCRIMINATOR) in (SYMPTOM, EMS)]
    if not relevant:
        return result

    result.warnings.append(
        Warning(
            "health_tracker:*:symptom",
            "defaulted",
            f"source records no severity; every occurrence gets {DEFAULT_SEVERITY!r}"
            f" (the EMS row gets {EMS_SEVERITY!r})",
        )
    )

    parents: dict[str, Record] = {}
    occurrences: list[Record] = []

    for row in relevant:
        key = row.key("symptom")
        occurred_at = parse_datetime(row.get(TIMESTAMP))
        if occurred_at is None:
            result.warnings.append(Warning(key, "skipped", "no parseable Timestamp"))
            continue

        known, freetext = tokenize_feelings(row.get(SYMPTOM_FEELING))
        names = list(known)
        if freetext:
            names.append(freetext if split_freetext else FREETEXT_SYMPTOM)
        if not names:
            names = [FREETEXT_SYMPTOM]

        is_ems = row.get(DISCRIMINATOR) == EMS
        notes = join_notes(
            freetext if not split_freetext else None,
            clean(row.get(REMARKS)),
            "Logged as an EMS-level episode." if is_ems else None,
        )

        for name in names:
            parent_key = f"symptom:{name.lower()}"
            existing = parents.get(parent_key)
            if existing is None:
                parents[parent_key] = Record(
                    parent_key,
                    "symptom",
                    "/symptoms/",
                    {
                        "patient_id": patient_id,
                        "symptom_name": name[:200],
                        "first_occurrence_date": occurred_at.date().isoformat(),
                        "status": "active",
                        "tags": [tag("gsheets-import")],
                    },
                )
            elif occurred_at.date().isoformat() < existing.payload["first_occurrence_date"]:
                existing.payload["first_occurrence_date"] = occurred_at.date().isoformat()

            occurrences.append(
                Record(
                    f"{key}:{parent_key}",
                    "symptom_occurrence",
                    "/symptoms/{parent_id}/occurrences",
                    {
                        "occurrence_date": occurred_at.date().isoformat(),
                        "occurrence_time": occurred_at.time().isoformat(),
                        "severity": EMS_SEVERITY if is_ems else DEFAULT_SEVERITY,
                        **({"notes": notes} if notes else {}),
                    },
                    parent_key=parent_key,
                )
            )

    result.records.extend(parents.values())
    result.records.extend(occurrences)
    return result


def map_sidecar(rows: list[Row]) -> MappingResult:
    """Everything MediKeep has no table for, kept verbatim.

    508 dose events, 8 sleep logs, 16 food entries, 1 fitness entry. The doses
    are the important ones: medications models "drugs I am on", not "times I
    swallowed something", so forcing them in would have produced a technically
    successful import and an unusable medication list.
    """
    result = MappingResult()
    buckets = {
        MEDICATION: "medication_log",
        SLEEP: "sleep_log",
        FOOD: "nutrition_log",
        FITNESS: "activity_log",
    }
    for row in rows:
        bucket = buckets.get(row.get(DISCRIMINATOR))
        if bucket is None:
            continue
        entry = {"source_key": row.key(bucket)}
        entry.update({k: v for k, v in row.data.items() if v and v.strip()})
        result.sidecar.setdefault(bucket, []).append(entry)
    return result
