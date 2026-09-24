"""The two things every mapper produces: records to POST, and warnings to read.

A mapper never talks to the network. It takes parsed CSV rows and returns these
dataclasses, which makes every mapping rule testable without a running MediKeep.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Record:
    """One POST the importer intends to make.

    source_key is the idempotency key: `<file>:<row_index>:<sub_entity>`. Row
    index is stable because a Forms response sheet only ever grows at the end.

    path may contain `{parent_id}`, which the applier fills from the record
    named by parent_key once that parent exists. Symptom occurrences and lab
    test components both need this -- and symptom parents are shared across
    many rows, so a parent/child tree would not have been enough.

    parent_field names a payload key that also needs the parent's id.
    LabTestComponentCreate requires lab_result_id in the body even though the
    path already carries it, and omitting it is a 422.
    """

    source_key: str
    entity: str
    path: str
    payload: dict
    parent_key: str | None = None
    parent_field: str | None = None


@dataclass(frozen=True)
class Warning:
    """Something the importer decided on its own. Every one of these prints."""

    source_key: str
    kind: str
    detail: str


@dataclass
class MappingResult:
    records: list[Record] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)
    sidecar: dict[str, list[dict]] = field(default_factory=dict)

    def extend(self, other: "MappingResult") -> None:
        self.records.extend(other.records)
        self.warnings.extend(other.warnings)
        for name, rows in other.sidecar.items():
            self.sidecar.setdefault(name, []).extend(rows)
