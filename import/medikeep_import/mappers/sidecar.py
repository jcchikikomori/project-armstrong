"""insights.csv and persona.csv -> derived JSON.

Neither file has a MediKeep home. insights.csv is narrative analysis
("Sodium Reduction (The ""Sawsawan"" Swap)"), persona.csv is habits,
personality, fitness history and goals in a key/value shape. Forcing them into
encounters would fill the visit history with non-visits, so they stay as JSON
that the LLM pipeline can read alongside dataset.json.

The single drug-allergy row in persona.csv is handled by mappers/allergies.py
and is excluded here, so it does not land in two places.
"""

from __future__ import annotations

from ..models import MappingResult
from ..normalize import squish
from ..sources import Row

DRUG_ALLERGY_ROW = "drug-triggered allergies"
PERSONA_ITEM = "Item"


def map_insights(rows: list[Row]) -> MappingResult:
    result = MappingResult()
    for row in rows:
        entry = {"source_key": row.key("insight")}
        entry.update({k: v for k, v in row.data.items() if v and v.strip()})
        result.sidecar.setdefault("insights", []).append(entry)
    return result


def map_persona(rows: list[Row]) -> MappingResult:
    result = MappingResult()
    for row in rows:
        if (squish(row.get(PERSONA_ITEM)) or "").lower() == DRUG_ALLERGY_ROW:
            continue
        entry = {"source_key": row.key("persona")}
        entry.update({k: v for k, v in row.data.items() if v and v.strip()})
        result.sidecar.setdefault("persona", []).append(entry)
    return result
