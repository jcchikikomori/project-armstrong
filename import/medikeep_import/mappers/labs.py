"""laboratory_log.csv -> lab results with test components.

The tidiest file of the seven: one row per test result, ISO dates, 125 rows
across 9 draw dates. Rows are grouped into panels by (date, normalized
category), because that is how the blood was actually drawn -- 14 hematology
values on 2026-05-12 came off one tube, not 14 separate orders.
"""

from __future__ import annotations

from collections import defaultdict

from ..models import MappingResult, Record, Warning
from ..normalize import (
    clean,
    join_notes,
    normalize_unit,
    parse_date,
    parse_ref_range,
    split_result,
    squish,
    tag,
)
from ..sources import Row

DATE = "Date"
CATEGORY = "Category"
TEST_NAME = "Test Name"
RESULT = "Result"
UNIT = "Unit"
REFERENCE = "Reference Range"
STATUS = "Status"
REMARKS = "Remarks"

# The sheet's categories, including three truncated ones (Chem, Sero, Ultra),
# mapped onto MediKeep's test_category vocabulary. Normalizing first also
# merges Chem into Clinical Chemistry on 2026-05-29, which is the same draw.
CATEGORY_MAP = {
    "hematology": ("hematology", "Hematology"),
    "chemistry": ("chemistry", "Chemistry"),
    "clinical chemistry": ("chemistry", "Chemistry"),
    "chem": ("chemistry", "Chemistry"),
    "clinical microscopy": ("other", "Clinical Microscopy"),
    "urinalysis": ("other", "Clinical Microscopy"),
    "immunology/serology": ("immunology", "Immunology/Serology"),
    "serology": ("immunology", "Immunology/Serology"),
    "sero": ("immunology", "Immunology/Serology"),
    "radiology": ("imaging", "Radiology"),
    "ultra": ("imaging", "Ultrasound"),
    "echo": ("cardiology", "Echocardiography"),
    "cardiovascular": ("cardiology", "Cardiovascular"),
}

# Sheet Status -> MediKeep labs_result enum.
STATUS_MAP = {"normal": "normal", "high": "high", "low": "low", "abnormal": "abnormal"}

# The component validator accepts only these four for qualitative_value, and
# this is not in the OpenAPI schema -- it is a Pydantic field validator, so the
# only way to find it is to POST and read the 422. Everything else that is not
# a number goes to textual_value instead.
QUALITATIVE_VALUES = frozenset({"positive", "negative", "detected", "undetected"})


def _category(raw: str, key: str, result: MappingResult) -> tuple[str, str]:
    mapped = CATEGORY_MAP.get((squish(raw) or "").lower())
    if mapped is None:
        result.warnings.append(Warning(key, "unmapped-category", f"{raw!r} -> other"))
        return "other", squish(raw) or "Laboratory"
    return mapped


def map_labs(rows: list[Row], patient_id: int) -> MappingResult:
    result = MappingResult()
    panels: dict[tuple[str, str], list[Row]] = defaultdict(list)
    labels: dict[tuple[str, str], tuple[str, str]] = {}

    for row in rows:
        key = row.key("lab")
        drawn = parse_date(row.get(DATE))
        if drawn is None:
            result.warnings.append(Warning(key, "skipped", "no parseable Date"))
            continue
        category, label = _category(row.get(CATEGORY), key, result)
        group = (drawn.isoformat(), category)
        panels[group].append(row)
        labels[group] = (category, label)

    for (drawn, category), members in sorted(panels.items()):
        _, label = labels[(drawn, category)]
        panel_key = f"laboratory_log:{drawn}:{category}"
        panel_payload = {
            "patient_id": patient_id,
            "test_name": f"{label} — {drawn}",
            "test_category": category,
            "status": "completed",
            "ordered_date": drawn,
            "completed_date": drawn,
            "is_panel": len(members) > 1,
            "tags": [tag("gsheets-import")],
        }
        notes = "\n".join(
            f"{squish(m.get(TEST_NAME))}: {squish(m.get(REMARKS))}"
            for m in members
            if clean(m.get(REMARKS))
        )
        if notes:
            panel_payload["notes"] = notes
        result.records.append(Record(panel_key, "lab_result", "/lab-results/", panel_payload))

        for order, member in enumerate(members):
            result.records.append(_component(member, panel_key, order, category, result))

    return result


def _component(
    row: Row, panel_key: str, order: int, category: str, result: MappingResult
) -> Record:
    key = row.key("lab_component")
    value, qualitative = split_result(row.get(RESULT))
    unit = normalize_unit(row.get(UNIT))
    low, high, text = parse_ref_range(row.get(REFERENCE))
    status = STATUS_MAP.get((squish(row.get(STATUS)) or "").lower())
    if status is None and clean(row.get(STATUS)):
        result.warnings.append(Warning(key, "unmapped-status", f"{row.get(STATUS)!r} dropped"))

    # MediKeep wants quantitative components to carry both a value and a unit.
    # `POSITIVE`, `Yellow` and `<1.5` have no honest float, so they go
    # qualitative rather than being coerced.
    quantitative = value is not None and unit is not None
    payload: dict = {
        "test_name": squish(row.get(TEST_NAME)) or "Unnamed test",
        "result_type": "quantitative" if quantitative else "qualitative",
        "display_order": order,
        # The normalized category, not the sheet's own label: the component
        # validator rejects anything outside its vocabulary, so `Clinical
        # Microscopy` and `Chem` would both 422.
        "category": category,
    }
    if quantitative:
        payload["value"] = value
        payload["unit"] = unit
        # A compound cell like `0.10 Nonreactive` carries a titre and its
        # interpretation. The titre is the value; the word would otherwise be
        # dropped on the floor, so it is kept verbatim.
        if qualitative:
            payload["textual_value"] = qualitative
    else:
        raw = qualitative or (str(value) if value is not None else None)
        canonical = (raw or "").strip().lower()
        if canonical in QUALITATIVE_VALUES:
            payload["qualitative_value"] = canonical
        elif raw:
            # `Yellow`, `Steatosis`, `Trace`, `<1.5` are all real results that
            # the four-value qualitative enum has no room for. textual_value
            # takes free text, so they keep their wording instead of being
            # forced into `positive`/`negative` or dropped.
            payload["textual_value"] = raw
        if value is not None and qualitative:
            payload["textual_value"] = f"{value} {qualitative}"
    # "Reference ranges are not applicable for qualitative tests" -- another
    # validator the schema does not advertise. The sheet still records a range
    # for those rows (`NEGATIVE`, `4.8 - 7.4`), so it moves to notes rather
    # than being thrown away.
    range_note = None
    if quantitative:
        if low is not None:
            payload["ref_range_min"] = low
        if high is not None:
            payload["ref_range_max"] = high
        if text is not None:
            payload["ref_range_text"] = text
    elif clean(row.get(REFERENCE)):
        range_note = f"Reference range: {squish(row.get(REFERENCE))}"

    if status is not None:
        payload["status"] = status
    notes = join_notes(clean(row.get(REMARKS)), range_note)
    if notes:
        payload["notes"] = notes

    return Record(
        key,
        "lab_test_component",
        "/lab-test-components/lab-result/{parent_id}/components",
        {k: v for k, v in payload.items() if v is not None},
        parent_key=panel_key,
        parent_field="lab_result_id",
    )
