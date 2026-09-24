from __future__ import annotations

from medikeep_import.mappers import labs


def _panels(result):
    return [r for r in result.records if r.entity == "lab_result"]


def _components(result):
    return [r for r in result.records if r.entity == "lab_test_component"]


def _by_test(result, name):
    return next(c for c in _components(result) if c.payload["test_name"] == name)


def test_rows_group_into_panels_by_date_and_normalized_category(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    names = sorted(p.payload["test_name"] for p in _panels(result))
    # Two hematology rows collapse into one panel; Chem, Sero, Clinical
    # Microscopy each get their own on the same date.
    assert names == [
        "Chemistry — 2024-01-06",
        "Clinical Microscopy — 2024-01-06",
        "Hematology — 2024-01-06",
        "Immunology/Serology — 2024-01-06",
        "Mystery — 2026-06-04",
        "Ultrasound — 2026-06-04",
    ]


def test_is_panel_is_false_for_a_single_component(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    by_name = {p.payload["test_name"]: p for p in _panels(result)}
    assert by_name["Hematology — 2024-01-06"].payload["is_panel"] is True
    assert by_name["Ultrasound — 2026-06-04"].payload["is_panel"] is False


def test_truncated_categories_map_onto_medikeeps_vocabulary(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    categories = {p.payload["test_name"]: p.payload["test_category"] for p in _panels(result)}
    assert categories["Chemistry — 2024-01-06"] == "chemistry"
    assert categories["Immunology/Serology — 2024-01-06"] == "immunology"
    assert categories["Ultrasound — 2026-06-04"] == "imaging"
    assert categories["Clinical Microscopy — 2024-01-06"] == "other"


def test_an_unknown_category_falls_back_to_other_with_a_warning(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    assert any(w.kind == "unmapped-category" for w in result.warnings)


def test_a_numeric_result_with_a_unit_is_quantitative(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    wbc = _by_test(result, "WBC")
    assert wbc.payload["result_type"] == "quantitative"
    assert wbc.payload["value"] == 7.65
    assert wbc.payload["unit"] == "10^9/L"
    assert wbc.payload["ref_range_min"] == 4.50
    assert wbc.payload["ref_range_max"] == 10.00


def test_unit_aliases_are_folded(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    assert _by_test(result, "RBC").payload["unit"] == "10^12/L"


def test_a_word_result_stays_qualitative(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    protein = _by_test(result, "Protein")
    assert protein.payload["result_type"] == "qualitative"
    # The validator accepts only positive/negative/detected/undetected, and it
    # is case-sensitive.
    assert protein.payload["qualitative_value"] == "positive"
    assert "value" not in protein.payload


def test_a_qualitative_component_keeps_its_range_in_notes(rows):
    # "Reference ranges are not applicable for qualitative tests" is a 422, so
    # the sheet's range moves to notes instead of being dropped.
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    protein = _by_test(result, "Protein")
    assert "ref_range_text" not in protein.payload
    assert "ref_range_min" not in protein.payload
    assert protein.payload["notes"] == "Reference range: NEGATIVE"


def test_a_result_outside_the_qualitative_enum_goes_to_textual_value(rows):
    # `Steatosis` is a real ultrasound finding with no home in a four-value
    # enum. Forcing it to `positive` would be a lie; dropping it loses the
    # result.
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    scan = _by_test(result, "Liver ultrasound")
    assert scan.payload["textual_value"] == "Steatosis"
    assert "qualitative_value" not in scan.payload


def test_components_carry_the_normalized_category_not_the_sheet_label(rows):
    # The sheet says "Clinical Microscopy" and "Chem"; the component validator
    # accepts neither.
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    assert _by_test(result, "Protein").payload["category"] == "other"
    assert _by_test(result, "Creatinine").payload["category"] == "chemistry"
    assert _by_test(result, "HBsAg").payload["category"] == "immunology"


def test_a_compound_cell_keeps_both_halves(rows):
    # `0.10 Nonreactive` in S/CO: the titre is the value, the word survives
    # alongside it instead of being dropped.
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    hbsag = _by_test(result, "HBsAg")
    assert hbsag.payload["result_type"] == "quantitative"
    assert hbsag.payload["value"] == 0.10
    assert hbsag.payload["unit"] == "S/CO"
    assert hbsag.payload["textual_value"] == "Nonreactive"


def test_a_compound_cell_without_a_unit_stays_qualitative():
    from medikeep_import.sources import Row

    row = Row(
        "laboratory_log",
        0,
        {"Date": "2026-01-01", "Category": "Sero", "Test Name": "HIV", "Result": "0.51 Nonreactive"},
    )
    result = labs.map_labs([row], patient_id=1)
    component = _components(result)[0]
    assert component.payload["result_type"] == "qualitative"
    assert component.payload["textual_value"] == "0.51 Nonreactive"
    assert "qualitative_value" not in component.payload


def test_an_inequality_range_keeps_its_text_alongside_the_bound(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    creatinine = _by_test(result, "Creatinine")
    assert creatinine.payload["ref_range_max"] == 106.0
    assert creatinine.payload["ref_range_text"] == "< 106"


def test_status_maps_and_an_unknown_one_warns(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    assert _by_test(result, "WBC").payload["status"] == "normal"
    assert _by_test(result, "Liver ultrasound").payload["status"] == "abnormal"
    assert "status" not in _by_test(result, "Unknown panel").payload
    assert any(w.kind == "unmapped-status" for w in result.warnings)


def test_component_remarks_surface_on_both_the_component_and_the_panel(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    assert _by_test(result, "Creatinine").payload["notes"] == "Fine, monitor lang."
    chemistry = next(
        p for p in _panels(result) if p.payload["test_name"] == "Chemistry — 2024-01-06"
    )
    assert chemistry.payload["notes"] == "Creatinine: Fine, monitor lang."


def test_components_point_at_their_panel_and_keep_their_order(rows):
    result = labs.map_labs(rows["laboratory_log"], patient_id=7)
    hematology = [c for c in _components(result) if c.parent_key.endswith(":hematology")]
    assert [c.payload["display_order"] for c in hematology] == [0, 1]
    assert all("{parent_id}" in c.path for c in hematology)


def test_a_row_without_a_date_is_skipped():
    from medikeep_import.sources import Row

    row = Row("laboratory_log", 0, {"Date": "", "Category": "Hematology", "Test Name": "WBC"})
    result = labs.map_labs([row], patient_id=1)
    assert result.records == []
    assert any(w.kind == "skipped" for w in result.warnings)


def test_a_component_with_no_test_name_still_gets_one():
    from medikeep_import.sources import Row

    row = Row("laboratory_log", 0, {"Date": "2026-01-01", "Category": "Hematology", "Test Name": ""})
    result = labs.map_labs([row], patient_id=1)
    assert _components(result)[0].payload["test_name"] == "Unnamed test"
