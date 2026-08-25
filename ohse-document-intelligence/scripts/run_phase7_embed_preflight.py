"""Execute scoped Phase 7 embed preflight (pages 46–49)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from persistence.phase7_embed_preflight import execute_embed_preflight

OUT_JSON = Path(r"E:\temp\ohse_phase7_embed_preflight_result.json")


def main() -> None:
    result = execute_embed_preflight(commit=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
