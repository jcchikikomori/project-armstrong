from __future__ import annotations

from pathlib import Path

import pytest

from medikeep_import.sources import load_all

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rows():
    """Synthetic CSVs that reproduce every quirk of the real export.

    The real datasets/*.csv are gitignored -- they hold actual medical
    records -- so the fixtures carry the shapes, not the data.
    """
    return load_all(FIXTURES)
