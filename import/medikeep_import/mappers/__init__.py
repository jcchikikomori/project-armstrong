"""Mappers: parsed CSV rows in, Records and Warnings out.

No mapper touches the network, which is what makes every rule in here
testable without a running MediKeep.
"""

from __future__ import annotations

from ..models import MappingResult
from ..sources import Row
from . import allergies, encounters, labs, medications, sidecar, tracker


def map_everything(
    rows_by_file: dict[str, list[Row]],
    patient_id: int,
    *,
    split_freetext: bool = False,
) -> MappingResult:
    """Run every mapper in dependency order.

    Order matters only in that parents precede the records that reference
    them, which each mapper already guarantees internally.
    """
    tracker_rows = rows_by_file.get("health_tracker", [])
    result = MappingResult()
    result.extend(tracker.map_vitals(tracker_rows, patient_id))
    result.extend(tracker.map_symptoms(tracker_rows, patient_id, split_freetext=split_freetext))
    result.extend(labs.map_labs(rows_by_file.get("laboratory_log", []), patient_id))
    result.extend(medications.map_medications(rows_by_file.get("medicine_tracking", []), patient_id))
    result.extend(allergies.map_assessments(rows_by_file.get("assessments", []), patient_id))
    result.extend(allergies.map_persona_allergy(rows_by_file.get("persona", []), patient_id))
    result.extend(encounters.map_encounters(rows_by_file.get("doctor_notes", []), patient_id))
    result.extend(tracker.map_sidecar(tracker_rows))
    result.extend(sidecar.map_insights(rows_by_file.get("insights", [])))
    result.extend(sidecar.map_persona(rows_by_file.get("persona", [])))
    return result


__all__ = ["map_everything", "allergies", "encounters", "labs", "medications", "sidecar", "tracker"]
