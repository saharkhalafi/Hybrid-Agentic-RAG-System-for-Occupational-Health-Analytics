"""Repair chemical identity links in the existing production database."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from agents.routing.chemical_registry_cache import reset_chemical_registry_cache
from database.session import SessionLocal
from persistence.chemical_identity import repair_chemical_identity_links


def main() -> int:
    session = SessionLocal()
    try:
        stats = repair_chemical_identity_links(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        reset_chemical_registry_cache()
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
