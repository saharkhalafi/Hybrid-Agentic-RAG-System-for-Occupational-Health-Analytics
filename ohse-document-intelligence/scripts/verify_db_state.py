"""Independent verification of canonical OEL DB counts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from sqlalchemy import create_engine, text

from config.settings import get_settings


def main() -> None:
    engine = create_engine(str(get_settings().database_url))
    with engine.connect() as conn:
        counts = {
            "accepted_canonical": conn.execute(
                text(
                    "SELECT COUNT(*) FROM oel_chemical_limits "
                    "WHERE validation_status = 'accepted' "
                    "AND gold_artifact_path = 'canonical_evidence_v1'"
                )
            ).scalar_one(),
            "legacy_reference": conn.execute(
                text("SELECT COUNT(*) FROM oel_chemical_limits WHERE validation_status = 'legacy_reference'")
            ).scalar_one(),
            "review_required": conn.execute(
                text("SELECT COUNT(*) FROM oel_chemical_limits WHERE validation_status = 'review_required'")
            ).scalar_one(),
            "canonical_blocked_build": conn.execute(
                text(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT source_row_key FROM oel_chemical_limits "
                    "  WHERE validation_status = 'review_required'"
                    ") q"
                )
            ).scalar_one(),
        }
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
