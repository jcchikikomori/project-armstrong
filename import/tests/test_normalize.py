from __future__ import annotations

from datetime import date, datetime

import pytest

from medikeep_import import normalize as n


@pytest.mark.parametrize("value", ["", "   ", "-", "N/A", "n/a", "NA", "na"])
def test_clean_treats_every_blank_convention_as_none(value):
    assert n.clean(value) is None


def test_clean_keeps_none_because_it_is_a_real_triggers_value():
    assert n.clean("None") == "None"


def test_clean_accepts_extra_blanks_per_column():
    assert n.clean("None", extra_blanks=("None",)) is None


def test_clean_preserves_newlines_in_free_text():
    assert n.clean("  line one\n\nline two  ") == "line one\n\nline two"


def test_clean_of_none_is_none():
    assert n.clean(None) is None


def test_squish_collapses_internal_whitespace():
    assert n.squish("Colecalciferol ") == "Colecalciferol"
    assert n.squish("4.03  mmol/L") == "4.03 mmol/L"
    assert n.squish("  ") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-01-06", datetime(2024, 1, 6)),
        ("1/18/2026 20:17:08", datetime(2026, 1, 18, 20, 17, 8)),
        ("6/21/2026 0:14:37", datetime(2026, 6, 21, 0, 14, 37)),
        ("5/6/2026", datetime(2026, 5, 6)),
    ],
)
def test_parse_datetime_handles_all_three_conventions(value, expected):
    assert n.parse_datetime(value) == expected


@pytest.mark.parametrize("value", ["", "not a date", "13/45/2026"])
def test_parse_datetime_returns_none_for_junk(value):
    assert n.parse_datetime(value) is None


def test_parse_date_narrows_to_date():
    assert n.parse_date("1/18/2026 20:17:08") == date(2026, 1, 18)
    assert n.parse_date("") is None


def test_parse_number_strips_thousands_separators():
    assert n.parse_number("1,300") == 1300.0
    assert n.parse_number("7.65") == 7.65
    assert n.parse_number("POSITIVE") is None
    assert n.parse_number("") is None


def test_parse_int_rejects_fractions():
    assert n.parse_int("129") == 129
    assert n.parse_int("-39") == -39
    assert n.parse_int("5.5") is None
    assert n.parse_int("") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7.65", (7.65, None)),
        ("0.10 Nonreactive", (0.10, "Nonreactive")),
        ("POSITIVE", (None, "POSITIVE")),
        ("<1.5", (None, "<1.5")),
        ("3-6", (None, "3-6")),
        ("", (None, None)),
    ],
)
def test_split_result(value, expected):
    assert n.split_result(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("4.50 - 10.00", (4.50, 10.00, None)),
        ("3.89-5.49", (3.89, 5.49, None)),
        ("1.00~7.00", (1.00, 7.00, None)),
        ("< 5.18", (None, 5.18, "< 5.18")),
        ("<1.70", (None, 1.70, "<1.70")),
        ("> 1.04", (1.04, None, "> 1.04")),
        ("≥60.0", (60.0, None, "≥60.0")),
        ("NEGATIVE", (None, None, "NEGATIVE")),
        ("-", (None, None, None)),
    ],
)
def test_parse_ref_range_covers_every_grammar(value, expected):
    assert n.parse_ref_range(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("109/L", "10^9/L"),
        ("X10^9/L", "10^9/L"),
        ("1012/L", "10^12/L"),
        ("/UL", "/uL"),
        ("mmol/L", "mmol/L"),
        ("N/A", None),
    ],
)
def test_normalize_unit(value, expected):
    assert n.normalize_unit(value) == expected


def test_tag_matches_medikeeps_charset_rule():
    assert n.tag("Trigger Assessment") == "trigger-assessment"
    assert n.tag("Gsheets  Import!!") == "gsheets-import"
    assert n.tag("") == ""
    assert len(n.tag("x" * 80)) == 50


def test_truncate_reports_the_cut():
    assert n.truncate("abc", 10) == ("abc", False)
    value, cut = n.truncate("a" * 20, 10)
    assert cut is True
    assert len(value) == 10
    assert n.truncate(None, 10) == (None, False)


def test_join_notes_drops_empties():
    assert n.join_notes("a", None, "", "  ", "b") == "a\n\nb"
    assert n.join_notes(None, "") is None
