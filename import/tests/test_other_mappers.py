from __future__ import annotations

import pytest

from medikeep_import.mappers import allergies, encounters, medications, sidecar
from medikeep_import.sources import Row

# -- medications ----------------------------------------------------------


def test_medications_map_type_and_status(rows):
    result = medications.map_medications(rows["medicine_tracking"], patient_id=7)
    by_name = {r.payload["medication_name"]: r.payload for r in result.records}
    assert by_name["Rosuvastatin"]["medication_type"] == "prescription"
    assert by_name["Rosuvastatin"]["status"] == "active"
    assert by_name["Doxycycline"]["medication_type"] == "otc"
    assert by_name["Doxycycline"]["status"] == "stopped"


def test_trailing_whitespace_in_a_drug_name_is_stripped(rows):
    result = medications.map_medications(rows["medicine_tracking"], patient_id=7)
    assert any(r.payload["medication_name"] == "Vitamin B Complex" for r in result.records)


def test_the_prescriber_lands_in_notes_rather_than_a_practitioner_record(rows):
    result = medications.map_medications(rows["medicine_tracking"], patient_id=7)
    rosuvastatin = next(r for r in result.records if r.payload["medication_name"] == "Rosuvastatin")
    assert "Prescribed by: Jane A. Doe, M.D." in rosuvastatin.payload["notes"]
    assert not any(r.entity == "practitioner" for r in result.records)


def test_tracker_counts_are_preserved_in_notes(rows):
    result = medications.map_medications(rows["medicine_tracking"], patient_id=7)
    rosuvastatin = next(r for r in result.records if r.payload["medication_name"] == "Rosuvastatin")
    assert "prescribed 200, taken 127, remaining 73" in rosuvastatin.payload["notes"]


def test_a_negative_remaining_is_warned_about(rows):
    result = medications.map_medications(rows["medicine_tracking"], patient_id=7)
    assert any(w.kind == "negative-remaining" for w in result.warnings)


def test_unknown_type_and_status_fall_back_with_warnings(rows):
    result = medications.map_medications(rows["medicine_tracking"], patient_id=7)
    vitamin = next(
        r for r in result.records if r.payload["medication_name"] == "Vitamin B Complex"
    )
    assert vitamin.payload["status"] == "active"
    mystery = next(r for r in result.records if r.payload["medication_name"] == "Mystery Drug")
    assert mystery.payload["medication_type"] == "prescription"
    assert any(w.kind == "unmapped-status" for w in result.warnings)
    assert any(w.kind == "unmapped-type" for w in result.warnings)


def test_a_row_without_a_generic_name_is_skipped(rows):
    result = medications.map_medications(rows["medicine_tracking"], patient_id=7)
    assert len(result.records) == 5
    assert any(w.kind == "skipped" for w in result.warnings)


def test_long_notes_are_cut_to_the_medication_cap():
    row = Row(
        "medicine_tracking",
        0,
        {
            medications.GENERIC: "Longwind",
            medications.TYPE: "Prescription",
            medications.STATUS: "In Progress",
            medications.REMARKS: "x" * 2000,
        },
    )
    result = medications.map_medications([row], patient_id=1)
    assert len(result.records[0].payload["notes"]) == medications.NOTES_LIMIT
    assert any(w.kind == "truncated" for w in result.warnings)


def test_a_row_with_no_counts_omits_the_counts_line():
    row = Row(
        "medicine_tracking",
        0,
        {
            medications.GENERIC: "Bare",
            medications.TYPE: "OTC",
            medications.STATUS: "Stopped",
        },
    )
    result = medications.map_medications([row], patient_id=1)
    assert "notes" not in result.records[0].payload


# -- allergies ------------------------------------------------------------


def test_assessments_become_allergies_with_the_intolerance_disclaimer(rows):
    result = allergies.map_assessments(rows["assessments"], patient_id=7)
    spicy = next(r for r in result.records if r.payload["allergen"] == "Spicy Sauce")
    assert allergies.DISCLAIMER in spicy.payload["notes"]
    assert "trigger-assessment" in spicy.payload["tags"]


def test_impact_maps_onto_the_severity_enum(rows):
    result = allergies.map_assessments(rows["assessments"], patient_id=7)
    severities = {r.payload["allergen"]: r.payload["severity"] for r in result.records}
    assert severities["Dust Mites"] == "life-threatening"
    assert severities["Spicy Sauce"] == "severe"
    assert severities["Wheat Bread"] == "moderate"  # "Unknown" falls back


def test_an_unmapped_impact_warns(rows):
    result = allergies.map_assessments(rows["assessments"], patient_id=7)
    assert any(w.kind == "unmapped-severity" for w in result.warnings)


def test_na_classification_cells_do_not_reach_the_notes(rows):
    result = allergies.map_assessments(rows["assessments"], patient_id=7)
    spicy = next(r for r in result.records if r.payload["allergen"] == "Spicy Sauce")
    assert "N/A" not in spicy.payload["notes"]
    assert "Classification: Food." in spicy.payload["notes"]


def test_a_none_trigger_is_treated_as_no_trigger(rows):
    result = allergies.map_assessments(rows["assessments"], patient_id=7)
    bread = next(r for r in result.records if r.payload["allergen"] == "Wheat Bread")
    assert "Trigger:" not in bread.payload["notes"]


def test_reaction_combines_affected_part_and_description(rows):
    result = allergies.map_assessments(rows["assessments"], patient_id=7)
    dust = next(r for r in result.records if r.payload["allergen"] == "Dust Mites")
    assert dust.payload["reaction"] == "Affects Nasal. Alikabok, Agiw"
    spicy = next(r for r in result.records if r.payload["allergen"] == "Spicy Sauce")
    assert spicy.payload["reaction"] == "Affects Throat."


def test_a_row_without_an_item_is_skipped(rows):
    result = allergies.map_assessments(rows["assessments"], patient_id=7)
    assert len(result.records) == 3
    assert any(w.kind == "skipped" for w in result.warnings)


def test_the_drug_allergy_waits_for_a_human_decision(rows):
    result = allergies.map_persona_allergy(rows["persona"], patient_id=7)
    if allergies.DRUG_ALLERGY_SEVERITY is None:
        assert result.records == []
        assert result.warnings[0].kind == "needs-decision"
    else:
        record = result.records[0]
        assert record.payload["allergen"] == "Fluoroquinolones"
        assert record.payload["reaction"] == "Due to Levofloxacin overdose"


def test_the_drug_allergy_splits_allergen_from_cause(monkeypatch, rows):
    monkeypatch.setattr(allergies, "DRUG_ALLERGY_SEVERITY", "moderate")
    result = allergies.map_persona_allergy(rows["persona"], patient_id=7)
    assert result.records[0].payload["allergen"] == "Fluoroquinolones"
    assert result.records[0].payload["reaction"] == "Due to Levofloxacin overdose"


def test_a_drug_allergy_without_a_cause_omits_the_reaction(monkeypatch):
    monkeypatch.setattr(allergies, "DRUG_ALLERGY_SEVERITY", "mild")
    row = Row(
        "persona",
        0,
        {allergies.PERSONA_ITEM: "Drug-triggered Allergies", allergies.PERSONA_VALUE: "Penicillin"},
    )
    result = allergies.map_persona_allergy([row], patient_id=1)
    assert "reaction" not in result.records[0].payload


def test_an_empty_drug_allergy_value_produces_nothing(monkeypatch):
    monkeypatch.setattr(allergies, "DRUG_ALLERGY_SEVERITY", "mild")
    row = Row(
        "persona", 0, {allergies.PERSONA_ITEM: "Drug-triggered Allergies", allergies.PERSONA_VALUE: ""}
    )
    assert allergies.map_persona_allergy([row], patient_id=1).records == []


# -- encounters -----------------------------------------------------------


def test_doctor_notes_become_encounters_with_multiline_notes(rows):
    result = encounters.map_encounters(rows["doctor_notes"], patient_id=7)
    assert len(result.records) == 1
    payload = result.records[0].payload
    assert payload["reason"] == "Consultation"
    assert payload["date"] == "2026-06-15"
    assert 'Line one\nLine two with "quotes" inside.' in payload["notes"]
    assert "Seen by: Jane A. Doe, M.D." in payload["notes"]
    assert payload["follow_up_instructions"] == "Follow up in\nAugust."


def test_an_encounter_without_a_date_is_skipped(rows):
    result = encounters.map_encounters(rows["doctor_notes"], patient_id=7)
    assert any(w.kind == "skipped" for w in result.warnings)


def test_long_encounter_notes_are_cut():
    row = Row(
        "doctor_notes",
        0,
        {
            encounters.ITEM: "Visit",
            encounters.REPORTED: "1/1/2026 10:00:00",
            encounters.DESCRIPTION: "x" * 6000,
        },
    )
    result = encounters.map_encounters([row], patient_id=1)
    assert len(result.records[0].payload["notes"]) == encounters.NOTES_LIMIT
    assert any(w.kind == "truncated" for w in result.warnings)


# -- sidecar --------------------------------------------------------------


def test_insights_go_to_sidecar_verbatim(rows):
    result = sidecar.map_insights(rows["insights"])
    entry = result.sidecar["insights"][0]
    assert entry["Insight title"] == 'Sodium Reduction (The "Swap")'
    assert "1,300 mg sodium" in entry["Description"]


def test_persona_skips_the_row_that_becomes_an_allergy(rows):
    result = sidecar.map_persona(rows["persona"])
    items = [e["Item"] for e in result.sidecar["persona"]]
    assert "Drug-triggered Allergies" not in items
    assert "My dislikes" in items


@pytest.mark.parametrize("mapper", [sidecar.map_insights, sidecar.map_persona])
def test_sidecar_mappers_create_no_records(rows, mapper):
    assert mapper(rows["persona"]).records == []
