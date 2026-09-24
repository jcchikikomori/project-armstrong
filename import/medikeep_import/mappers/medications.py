"""medicine_tracking.csv -> medications.

Seven drugs. This is the one file whose shape already matches MediKeep: one row
per drug, not per dose. The 508 dose events from the tracker go to sidecar JSON
instead -- see mappers/tracker.py.
"""

from __future__ import annotations

from ..models import MappingResult, Record, Warning
from ..normalize import clean, join_notes, parse_int, squish, tag, truncate
from ..sources import Row

TYPE = "Type"
GENERIC = "Generic Name"
BRAND = "Brand Name"
FORMULATION = "Formulation"
STATUS = "Status"
REMARKS = "Description/Remarks"
PRESCRIBED_QTY = "Prescribed Quantity"
REMAINING = "Remaining"
TAKEN = "Amount of Meds taken"
PRESCRIBER = "Prescribed by"

TYPE_MAP = {
    "prescription": "prescription",
    "otc": "otc",
    "supplement": "supplement",
    "herbal": "herbal",
}
STATUS_MAP = {"in progress": "active", "stopped": "stopped"}

# app/schemas/medication.py caps notes at 1000 characters, unlike the 5000 most
# other entities allow.
NOTES_LIMIT = 1000


def map_medications(rows: list[Row], patient_id: int) -> MappingResult:
    result = MappingResult()
    for row in rows:
        key = row.key("medication")
        name = squish(row.get(GENERIC))
        if not name:
            result.warnings.append(Warning(key, "skipped", "no Generic Name"))
            continue

        medication_type = TYPE_MAP.get((squish(row.get(TYPE)) or "").lower())
        if medication_type is None:
            result.warnings.append(
                Warning(key, "unmapped-type", f"{row.get(TYPE)!r} -> prescription")
            )
            medication_type = "prescription"

        status = STATUS_MAP.get((squish(row.get(STATUS)) or "").lower())
        if status is None:
            result.warnings.append(Warning(key, "unmapped-status", f"{row.get(STATUS)!r} -> active"))
            status = "active"

        remaining = parse_int(row.get(REMAINING))
        if remaining is not None and remaining < 0:
            # Over-consumption against the prescribed count. Worth surfacing
            # rather than quietly importing.
            result.warnings.append(
                Warning(key, "negative-remaining", f"{name}: Remaining={remaining}")
            )

        # No practitioner records are created: two of the three prescribers are
        # doctors and the third is a health centre, and creating a practitioner
        # needs a specialty_id whose endpoint is capped at 20/hour. The name is
        # kept where a human will read it.
        prescriber = squish(row.get(PRESCRIBER))
        counts = _counts_line(row)
        notes, was_cut = truncate(
            join_notes(
                clean(row.get(REMARKS)),
                f"Prescribed by: {prescriber}" if prescriber else None,
                counts,
            ),
            NOTES_LIMIT,
        )
        if was_cut:
            result.warnings.append(Warning(key, "truncated", f"notes cut to {NOTES_LIMIT} chars"))

        payload = {
            "patient_id": patient_id,
            "medication_name": name,
            "medication_type": medication_type,
            "status": status,
            "tags": [tag("gsheets-import")],
        }
        brand = squish(row.get(BRAND))
        if brand:
            payload["alternative_name"] = brand
        dosage = squish(row.get(FORMULATION))
        if dosage:
            payload["dosage"] = dosage
        if notes:
            payload["notes"] = notes

        result.records.append(Record(key, "medication", "/medications/", payload))
    return result


def _counts_line(row: Row) -> str | None:
    prescribed = parse_int(row.get(PRESCRIBED_QTY))
    remaining = parse_int(row.get(REMAINING))
    taken = parse_int(row.get(TAKEN))
    if prescribed is None and remaining is None and taken is None:
        return None
    parts = [
        f"prescribed {prescribed}" if prescribed is not None else None,
        f"taken {taken}" if taken is not None else None,
        f"remaining {remaining}" if remaining is not None else None,
    ]
    return "Tracker counts: " + ", ".join(p for p in parts if p) + "."
