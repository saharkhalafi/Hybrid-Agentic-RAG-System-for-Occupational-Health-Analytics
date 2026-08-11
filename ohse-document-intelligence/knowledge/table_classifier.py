"""Rule-based OHSE table type classifier."""

from __future__ import annotations

import re

from database.models import TableType

CAS_PATTERN = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
CHEMICAL_KEYWORDS = re.compile(r"\b(TWA|STEL|OEL|ceiling|ppm|mg/m3|mg/m³)\b", re.IGNORECASE)
VIBRATION_KEYWORDS = re.compile(r"\b(A\(8\)|VDV|vibration|hand-arm|whole-body)\b", re.IGNORECASE)
NOISE_KEYWORDS = re.compile(r"\b(LAeq|dBA|dose|criterion|noise)\b", re.IGNORECASE)
BIO_KEYWORDS = re.compile(r"\b(BEI|biological|specimen|urine|blood)\b", re.IGNORECASE)


def classify_table_text(text: str) -> TableType:
    if not text.strip():
        return TableType.UNKNOWN

    cas_hits = len(CAS_PATTERN.findall(text))
    chemical_hits = len(CHEMICAL_KEYWORDS.findall(text))
    vibration_hits = len(VIBRATION_KEYWORDS.findall(text))
    noise_hits = len(NOISE_KEYWORDS.findall(text))
    bio_hits = len(BIO_KEYWORDS.findall(text))

    scores = {
        TableType.CHEMICAL_OEL: cas_hits * 2 + chemical_hits,
        TableType.VIBRATION: vibration_hits,
        TableType.NOISE: noise_hits,
        TableType.BIOLOGICAL_MONITORING: bio_hits,
    }
    best_type, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score == 0:
        return TableType.UNKNOWN
    return best_type
