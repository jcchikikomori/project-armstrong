"""Value cleaners shared by every mapper.

Each Google Sheets tab carries its own conventions for dates, blanks and units.
laboratory_log.csv alone spells "no value" four ways in a single column. Every
cell-to-Python conversion lives here so a quirk gets fixed once, not per mapper.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# laboratory_log.csv uses ISO. The Forms export uses US order with unpadded
# month, day and hour (strptime accepts unpadded input for %m/%d/%H).
# insights.csv drops the time entirely.
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y",
)

# Blank conventions observed across the corpus. "None" is deliberately absent:
# in assessments.csv "None" is a real Triggers value meaning "nothing triggers
# this", not a missing cell.
_BLANKS = {"", "-", "n/a", "na"}

# laboratory_log.csv spells the same unit several ways.
_UNIT_ALIASES = {
    "109/L": "10^9/L",
    "X10^9/L": "10^9/L",
    "1012/L": "10^12/L",
    "X10^12/L": "10^12/L",
    "/UL": "/uL",
}

_NUMBER = r"[-+]?\d+(?:\.\d+)?"
_TWO_SIDED = re.compile(rf"^({_NUMBER})\s*[-~]\s*({_NUMBER})$")
_UPPER_BOUND = re.compile(rf"^[<≤]\s*({_NUMBER})$")
_LOWER_BOUND = re.compile(rf"^[>≥]\s*({_NUMBER})$")
_LEADING_NUMBER = re.compile(rf"^({_NUMBER})\s+(.+)$")
_TAG_STRIP = re.compile(r"[^a-z0-9.:-]")


def clean(value: str | None, *, extra_blanks: tuple[str, ...] = ()) -> str | None:
    """Strip a cell and map every blank convention to None.

    Internal whitespace and newlines survive: several Description cells are
    multi-line and the line breaks carry meaning.
    """
    if value is None:
        return None
    text = value.strip()
    lowered = text.lower()
    if lowered in _BLANKS or lowered in {b.lower() for b in extra_blanks}:
        return None
    return text or None


def squish(value: str | None) -> str | None:
    """clean(), then collapse internal whitespace.

    For names and grouping keys, where `Colecalciferol ` and `Colecalciferol`
    must land in the same bucket.
    """
    text = clean(value)
    return " ".join(text.split()) if text else None


def parse_datetime(value: str | None) -> datetime | None:
    text = squish(value)
    if not text:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: str | None) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def parse_number(value: str | None) -> float | None:
    text = squish(value)
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    number = parse_number(value)
    return int(number) if number is not None and number == int(number) else None


def split_result(value: str | None) -> tuple[float | None, str | None]:
    """Split a lab Result cell into a numeric part and a qualitative part.

    Handles the compound cells: `0.10 Nonreactive` carries both a titre and its
    interpretation. `<1.5` and `3-6` stay textual, because a single float would
    misrepresent a bound or a range as a measurement.
    """
    text = squish(value)
    if not text:
        return None, None
    number = parse_number(text)
    if number is not None:
        return number, None
    match = _LEADING_NUMBER.match(text)
    if match:
        return float(match.group(1)), match.group(2).strip()
    return None, text


def parse_ref_range(value: str | None) -> tuple[float | None, float | None, str | None]:
    """Parse a Reference Range cell into (min, max, fallback text).

    Five grammars appear in laboratory_log.csv: `4.50 - 10.00`, `3.89-5.49`,
    `1.00~7.00`, `< 5.18` and `> 1.04`, plus one `≥60.0`. Anything else
    (`NEGATIVE`, `Yellow`) is kept verbatim as text.
    """
    text = squish(value)
    if not text:
        return None, None, None
    match = _TWO_SIDED.match(text)
    if match:
        return float(match.group(1)), float(match.group(2)), None
    match = _UPPER_BOUND.match(text)
    if match:
        return None, float(match.group(1)), text
    match = _LOWER_BOUND.match(text)
    if match:
        return float(match.group(1)), None, text
    return None, None, text


def normalize_unit(value: str | None) -> str | None:
    text = squish(value)
    if not text:
        return None
    return _UNIT_ALIASES.get(text, text)


def tag(value: str) -> str:
    """Coerce a label into a tag MediKeep will accept.

    app/schemas/base_tags.py lowercases, turns spaces into hyphens and rejects
    anything outside [a-z0-9.-:]. Doing it here keeps the 422 from happening.
    """
    slug = _TAG_STRIP.sub("", squish(value).lower().replace(" ", "-") if squish(value) else "")
    return re.sub(r"-{2,}", "-", slug).strip("-")[:50]


def truncate(value: str | None, limit: int) -> tuple[str | None, bool]:
    """Cut a note to the entity's cap, reporting whether anything was lost."""
    if value is None:
        return None, False
    if len(value) <= limit:
        return value, False
    return value[: limit - 3].rstrip() + "...", True


def join_notes(*parts: str | None) -> str | None:
    kept = [p.strip() for p in parts if p and p.strip()]
    return "\n\n".join(kept) if kept else None
