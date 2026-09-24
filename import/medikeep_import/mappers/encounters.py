"""doctor_notes.csv -> encounters.

Two records, both multi-line markdown-ish text with escaped quotes and bare
URLs inside quoted cells. Small file, but the one that proves the parser has
to be RFC-4180: `wc -l` says 21 lines for these 2 rows.
"""

from __future__ import annotations

from ..models import MappingResult, Record, Warning
from ..normalize import clean, join_notes, parse_date, squish, tag, truncate
from ..sources import Row

ITEM = "Item"
DESCRIPTION = "Description"
PRACTITIONER = "Doctor/Specialist"
REPORTED = "Reported Date"
REMARKS = "Remarks"

NOTES_LIMIT = 5000


def map_encounters(rows: list[Row], patient_id: int) -> MappingResult:
    result = MappingResult()
    for row in rows:
        key = row.key("encounter")
        reason = squish(row.get(ITEM))
        visited = parse_date(row.get(REPORTED))
        if not reason or visited is None:
            result.warnings.append(Warning(key, "skipped", "needs both Item and Reported Date"))
            continue

        practitioner = squish(row.get(PRACTITIONER))
        notes, was_cut = truncate(
            join_notes(
                clean(row.get(DESCRIPTION)),
                f"Seen by: {practitioner}" if practitioner else None,
            ),
            NOTES_LIMIT,
        )
        if was_cut:
            result.warnings.append(Warning(key, "truncated", f"notes cut to {NOTES_LIMIT} chars"))

        payload = {
            "patient_id": patient_id,
            "reason": reason[:500],
            "date": visited.isoformat(),
            "visit_type": "consultation",
            "tags": [tag("gsheets-import")],
        }
        if notes:
            payload["notes"] = notes
        follow_up, _ = truncate(clean(row.get(REMARKS)), NOTES_LIMIT)
        if follow_up:
            payload["follow_up_instructions"] = follow_up

        result.records.append(Record(key, "encounter", "/encounters/", payload))
    return result
