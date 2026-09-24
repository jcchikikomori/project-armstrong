from __future__ import annotations

from medikeep_import.mappers import tracker
from medikeep_import.sources import Row


def _records(result, entity):
    return [r for r in result.records if r.entity == entity]


def _warnings(result, kind):
    return [w for w in result.warnings if w.kind == kind]


# -- the multi-select tokenizer ------------------------------------------


def test_tokenizer_does_not_split_an_option_that_contains_a_comma():
    known, free = tracker.tokenize_feelings(
        'Aches (mostly Headache, otherwise use "Other"), Sakit sa Puson'
    )
    assert known == ['Aches (mostly Headache, otherwise use "Other")']
    assert free == "Sakit sa Puson"


def test_tokenizer_keeps_a_comma_laden_free_text_tail_whole():
    known, free = tracker.tokenize_feelings(
        "Dizzy/Nahihilo, Anxiety Trigger (Anxious), Nilalamig sa aircon, bit of a headache"
    )
    assert known == ["Dizzy/Nahihilo", "Anxiety Trigger (Anxious)"]
    assert free == "Nilalamig sa aircon, bit of a headache"


def test_tokenizer_handles_a_lone_option():
    assert tracker.tokenize_feelings("Chest Pains") == (["Chest Pains"], None)


def test_tokenizer_treats_an_unknown_leading_value_as_free_text():
    known, free = tracker.tokenize_feelings("Bit of chest pain, about to pee, anxious")
    assert known == []
    assert free == "Bit of chest pain, about to pee, anxious"


def test_tokenizer_on_empty_cell():
    assert tracker.tokenize_feelings("") == ([], None)


# -- vitals ---------------------------------------------------------------


def test_vitals_maps_only_bp_rows(rows):
    result = tracker.map_vitals(rows["health_tracker"], patient_id=7)
    assert len(_records(result, "vitals")) == 2


def test_vitals_drops_an_impossible_spo2_but_keeps_the_row(rows):
    result = tracker.map_vitals(rows["health_tracker"], patient_id=7)
    first = _records(result, "vitals")[0]
    assert "oxygen_saturation" not in first.payload
    assert first.payload["systolic_bp"] == 129
    assert first.payload["heart_rate"] == 98
    assert _warnings(result, "out-of-range")[0].detail.startswith("oxygen_saturation=119")


def test_vitals_keeps_a_plausible_spo2(rows):
    result = tracker.map_vitals(rows["health_tracker"], patient_id=7)
    assert _records(result, "vitals")[1].payload["oxygen_saturation"] == 97


def test_vitals_honours_a_backdate_hidden_in_the_period_enum(rows):
    result = tracker.map_vitals(rows["health_tracker"], patient_id=7)
    second = _records(result, "vitals")[1]
    assert second.payload["recorded_date"].startswith("2026-05-12T09:05:00")
    assert "submitted 2026-05-12 09:05:00" in second.payload["notes"]
    assert _warnings(result, "backdated")


def test_vitals_sends_a_datetime_not_a_date(rows):
    result = tracker.map_vitals(rows["health_tracker"], patient_id=7)
    assert "T" in _records(result, "vitals")[0].payload["recorded_date"]


def test_vitals_carries_the_import_source_marker(rows):
    result = tracker.map_vitals(rows["health_tracker"], patient_id=7)
    assert all(r.payload["import_source"] == tracker.IMPORT_SOURCE for r in result.records)


def test_vitals_skips_a_row_with_an_unparseable_timestamp():
    row = Row("health_tracker", 0, {tracker.DISCRIMINATOR: tracker.BP, tracker.TIMESTAMP: "junk"})
    result = tracker.map_vitals([row], patient_id=1)
    assert result.records == []
    assert _warnings(result, "skipped")


def test_vitals_keeps_a_plain_period_as_a_note():
    row = Row(
        "health_tracker",
        0,
        {
            tracker.DISCRIMINATOR: tracker.BP,
            tracker.TIMESTAMP: "1/1/2026 10:00:00",
            tracker.PERIOD: "Morning",
        },
    )
    result = tracker.map_vitals([row], patient_id=1)
    assert result.records[0].payload["notes"] == "Morning"


# -- symptoms -------------------------------------------------------------


def test_symptoms_group_free_text_by_default(rows):
    result = tracker.map_symptoms(rows["health_tracker"], patient_id=7)
    names = {r.payload["symptom_name"] for r in _records(result, "symptom")}
    assert tracker.FREETEXT_SYMPTOM in names
    assert "Numbness on my left pointy finger" not in names


def test_symptoms_can_split_free_text_instead(rows):
    result = tracker.map_symptoms(rows["health_tracker"], patient_id=7, split_freetext=True)
    names = {r.payload["symptom_name"] for r in _records(result, "symptom")}
    assert "Numbness on my left pointy finger" in names


def test_symptom_parents_are_shared_and_occurrences_point_back(rows):
    result = tracker.map_symptoms(rows["health_tracker"], patient_id=7)
    parents = {r.source_key for r in _records(result, "symptom")}
    occurrences = _records(result, "symptom_occurrence")
    assert occurrences
    assert all(o.parent_key in parents for o in occurrences)
    assert all("{parent_id}" in o.path for o in occurrences)


def test_parents_are_emitted_before_their_occurrences(rows):
    result = tracker.map_symptoms(rows["health_tracker"], patient_id=7)
    entities = [r.entity for r in result.records]
    assert entities.index("symptom") < entities.index("symptom_occurrence")


def test_first_occurrence_date_walks_backwards_to_the_earliest_row():
    early = Row(
        "health_tracker",
        0,
        {
            tracker.DISCRIMINATOR: tracker.SYMPTOM,
            tracker.TIMESTAMP: "3/1/2026 10:00:00",
            tracker.SYMPTOM_FEELING: "Chest Pains",
        },
    )
    earlier = Row(
        "health_tracker",
        1,
        {
            tracker.DISCRIMINATOR: tracker.SYMPTOM,
            tracker.TIMESTAMP: "1/1/2026 10:00:00",
            tracker.SYMPTOM_FEELING: "Chest Pains",
        },
    )
    result = tracker.map_symptoms([early, earlier], patient_id=1)
    parent = _records(result, "symptom")[0]
    assert parent.payload["first_occurrence_date"] == "2026-01-01"


def test_the_ems_row_gets_the_critical_severity(rows):
    result = tracker.map_symptoms(rows["health_tracker"], patient_id=7)
    severities = {o.payload["severity"] for o in _records(result, "symptom_occurrence")}
    assert tracker.EMS_SEVERITY in severities
    assert tracker.DEFAULT_SEVERITY in severities


def test_the_defaulted_severity_is_warned_about_once(rows):
    result = tracker.map_symptoms(rows["health_tracker"], patient_id=7)
    assert len(_warnings(result, "defaulted")) == 1


def test_symptoms_skip_an_unparseable_timestamp():
    row = Row(
        "health_tracker",
        0,
        {tracker.DISCRIMINATOR: tracker.SYMPTOM, tracker.TIMESTAMP: "", tracker.SYMPTOM_FEELING: "x"},
    )
    result = tracker.map_symptoms([row], patient_id=1)
    assert _records(result, "symptom") == []
    assert _warnings(result, "skipped")


def test_a_row_with_no_feelings_cell_still_gets_an_occurrence():
    row = Row(
        "health_tracker",
        0,
        {tracker.DISCRIMINATOR: tracker.SYMPTOM, tracker.TIMESTAMP: "1/1/2026 10:00:00"},
    )
    result = tracker.map_symptoms([row], patient_id=1)
    assert _records(result, "symptom")[0].payload["symptom_name"] == tracker.FREETEXT_SYMPTOM


def test_no_symptom_rows_means_no_warning_noise():
    result = tracker.map_symptoms([], patient_id=1)
    assert result.records == []
    assert result.warnings == []


# -- sidecar --------------------------------------------------------------


def test_sidecar_buckets_everything_medikeep_cannot_hold(rows):
    result = tracker.map_sidecar(rows["health_tracker"])
    assert len(result.sidecar["medication_log"]) == 1
    assert len(result.sidecar["sleep_log"]) == 1
    assert "vitals" not in result.sidecar


def test_sidecar_entries_keep_their_source_key_and_drop_empty_columns(rows):
    entry = tracker.map_sidecar(rows["health_tracker"]).sidecar["medication_log"][0]
    assert entry["source_key"].endswith(":medication_log")
    assert entry["What brand?"] == "Vitamin B Complex"
    assert "" not in entry.values()
