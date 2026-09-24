"""assessments.csv -> allergies, plus the one real drug allergy in persona.csv.

A caveat worth being blunt about: assessments.csv is a food and environment
*trigger* matrix -- dark chocolate, white rice, coffee -- and those are
intolerances, not IgE allergies. MediKeep has no intolerance table, so they
land in allergies, but every record says so in its own notes and carries the
`trigger-assessment` tag. Nobody reading the chart should mistake Lechon Baboy
for an anaphylaxis risk.
"""

from __future__ import annotations

from ..models import MappingResult, Record, Warning
from ..normalize import clean, join_notes, squish, tag
from ..sources import Row

ITEM = "Item"
HIGH_LEVEL = "Type (High Level)"
LEVEL_2 = "Type (Level 2)"
LEVEL_3 = "Type (Level 3)"
AFFECTED = "Affected Part"
DESCRIPTION = "Description"
TRIGGERS = "Triggers"
URIC_ACID = "Uric Acid levels"
RESOLUTION = "Resolution"
IMPACT = "Impact/Risk"
REMARKS = "Remarks"

PERSONA_ITEM = "Item"
PERSONA_VALUE = "Description/Value"
DRUG_ALLERGY_ROW = "drug-triggered allergies"

DISCLAIMER = (
    "Self-reported trigger from personal health tracking. "
    "Not a clinically confirmed allergy."
)

# Impact/Risk is the tracker's own five-level scale. MediKeep's severity enum
# is none|mild|moderate|severe|life-threatening.
SEVERITY_MAP = {
    "critical": "life-threatening",
    "high": "severe",
    "medium": "moderate",
    "low": "mild",
    "minimal": "mild",
}

# Severity for the persona.csv drug allergy ("Fluoroquinolones due to
# Levofloxacin overdose"). MediKeep requires one and the source records none,
# so this is the patient's own call, recorded here rather than guessed.
# Set to None to skip that single record with a warning instead.
DRUG_ALLERGY_SEVERITY: str | None = "mild"


def map_assessments(rows: list[Row], patient_id: int) -> MappingResult:
    result = MappingResult()
    for row in rows:
        key = row.key("allergy")
        allergen = squish(row.get(ITEM))
        if not allergen:
            result.warnings.append(Warning(key, "skipped", "no Item"))
            continue

        severity = SEVERITY_MAP.get((squish(row.get(IMPACT)) or "").lower())
        if severity is None:
            result.warnings.append(
                Warning(key, "unmapped-severity", f"{row.get(IMPACT)!r} -> moderate")
            )
            severity = "moderate"

        # "N/A" is a real string in these columns, so clean() drops it.
        classification = " / ".join(
            p for p in (clean(row.get(HIGH_LEVEL)), clean(row.get(LEVEL_2)), clean(row.get(LEVEL_3))) if p
        )
        triggers = clean(row.get(TRIGGERS), extra_blanks=("None",))
        payload = {
            "patient_id": patient_id,
            "allergen": allergen[:200],
            "severity": severity,
            "status": "active",
            "tags": [tag("gsheets-import"), tag("trigger-assessment")],
            "notes": join_notes(
                DISCLAIMER,
                f"Classification: {classification}." if classification else None,
                f"Trigger: {triggers}." if triggers else None,
                f"Uric acid impact: {clean(row.get(URIC_ACID))}."
                if clean(row.get(URIC_ACID))
                else None,
                f"Resolution: {clean(row.get(RESOLUTION))}"
                if clean(row.get(RESOLUTION))
                else None,
                clean(row.get(REMARKS)),
            ),
        }
        reaction = _reaction(row)
        if reaction:
            payload["reaction"] = reaction[:500]

        result.records.append(Record(key, "allergy", "/allergies/", payload))
    return result


def _reaction(row: Row) -> str | None:
    affected = clean(row.get(AFFECTED))
    description = clean(row.get(DESCRIPTION))
    if affected and description:
        return f"Affects {affected}. {description}"
    if affected:
        return f"Affects {affected}."
    return description


def map_persona_allergy(rows: list[Row], patient_id: int) -> MappingResult:
    """The single clinically meaningful row in persona.csv.

    Everything else in that file is habits, personality and goals, which goes
    to sidecar JSON.
    """
    result = MappingResult()
    for row in rows:
        if (squish(row.get(PERSONA_ITEM)) or "").lower() != DRUG_ALLERGY_ROW:
            continue
        key = row.key("allergy")
        value = squish(row.get(PERSONA_VALUE))
        if not value:
            continue
        if DRUG_ALLERGY_SEVERITY is None:
            result.warnings.append(
                Warning(
                    key,
                    "needs-decision",
                    "drug allergy skipped: set DRUG_ALLERGY_SEVERITY in mappers/allergies.py",
                )
            )
            continue

        allergen, _, cause = value.partition(" due to ")
        result.records.append(
            Record(
                key,
                "allergy",
                "/allergies/",
                {
                    "patient_id": patient_id,
                    "allergen": allergen.strip()[:200],
                    "severity": DRUG_ALLERGY_SEVERITY,
                    "status": "active",
                    "tags": [tag("gsheets-import")],
                    "reaction": (f"Due to {cause.strip()}" if cause else None),
                    "notes": "Self-reported drug allergy from personal health tracking.",
                },
            )
        )
    for record in result.records:
        if record.payload.get("reaction") is None:
            record.payload.pop("reaction", None)
    return result
