"""Reading the CSVs.

Five of the seven files carry bare LF inside quoted fields -- `wc -l` claims
doctor_notes.csv has 21 lines when it holds 2 records -- so parsing is
RFC-4180 via csv.DictReader and never line-based. All seven are CRLF with no
trailing newline and no BOM, but utf-8-sig is used anyway in case a future
export picks one up.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

FILES = (
    "health_tracker",
    "laboratory_log",
    "medicine_tracking",
    "assessments",
    "doctor_notes",
    "insights",
    "persona",
)


@dataclass(frozen=True)
class Row:
    """A CSV row plus where it came from.

    index is the 0-based position among data rows, and it is what makes
    source_key stable: a Forms response sheet only ever appends.
    """

    source: str
    index: int
    data: dict[str, str]

    def key(self, sub_entity: str) -> str:
        return f"{self.source}:{self.index}:{sub_entity}"

    def get(self, column: str) -> str:
        return self.data.get(column, "") or ""


def load(path: Path) -> list[Row]:
    source = path.stem
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [Row(source, i, dict(raw)) for i, raw in enumerate(reader)]


def load_all(datasets_dir: Path) -> dict[str, list[Row]]:
    """Load every known CSV. A missing file is an empty list, not an error.

    That keeps `plan` useful when only some tabs have been exported.
    """
    loaded: dict[str, list[Row]] = {}
    for name in FILES:
        path = datasets_dir / f"{name}.csv"
        loaded[name] = load(path) if path.exists() else []
    return loaded
